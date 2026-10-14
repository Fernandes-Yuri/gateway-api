"""Diagnóstico mínimo do WhatsApp e expurgo de arquivos ativos do canal."""
import fcntl
import json
import os
from pathlib import Path
from contextlib import contextmanager

from services.wa_channel_privacy import phone_hash


@contextmanager
def file_lock(path):
    lock_path = Path(str(path) + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def event_metadata(payload):
    events = []
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            for message in value.get('messages', []):
                phone = message.get('from')
                if phone:
                    events.append({'message_id': message.get('id'), 'type': message.get('type'),
                                   'phone_hash': phone_hash(phone)})
            for status in value.get('statuses', []):
                phone = status.get('recipient_id')
                if phone:
                    events.append({'message_id': status.get('id'), 'status': status.get('status'),
                                   'phone_hash': phone_hash(phone)})
    return {'events': events, 'content_redacted': True}


def append_event(path, record):
    with file_lock(path):
        with open(path, 'a', encoding='utf-8') as target:
            target.write(json.dumps(record, ensure_ascii=False) + '\n')
        os.chmod(path, 0o600)


def _rewrite(path, transform):
    path = Path(path)
    if not path.exists():
        return
    with file_lock(path):
        temp = path.with_name(path.name + '.privacy.tmp')
        try:
            with path.open(encoding='utf-8') as source, temp.open('w', encoding='utf-8') as target:
                for line in source:
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        # Cannot certify deletion of an unknown record. Keep
                        # the original file and retry the persistent cleanup job.
                        raise ValueError('Arquivo de diagnóstico inválido') from None
                    value = transform(value)
                    if value is not None:
                        target.write(json.dumps(value, ensure_ascii=False) + '\n')
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()


def purge_channel_files(phone=None, *, subject_key=None, outbox_path, trace_path, dataset_path):
    """Remove dados identificáveis do canal; mantém dados de outros canais.

    Arquivos legados WA sem chave do titular são minimizados: o conteúdo de
    conversa é removido, sem tentar atribuir texto a alguém por coincidência.
    Logs de sistema/backup têm retenção própria e não são varridos aqui.
    """
    key = subject_key or phone_hash(phone)

    def outbox(record):
        payload = record.get('payload') or {}
        if 'entry' in payload:
            # Legacy diagnostics are reduced to technical metadata first.
            payload = event_metadata(payload)
        events = [event for event in payload.get('events', []) if event.get('phone_hash') != key]
        if not events:
            return None
        return {'ts': record.get('ts'), 'payload': {'events': events, 'content_redacted': True}}

    def trace(record):
        if record.get('channel') != 'whatsapp':
            return record
        fields = record.get('fields') or {}
        if fields.get('phone_hash') == key:
            return None
        if not fields.get('phone_hash'):
            # Legacy records have only four phone digits, not enough to assign
            # ownership. Strip all conversational fields; keep diagnostic stage.
            return {k: record[k] for k in ('ts', 'trace_id', 'channel', 'stage') if k in record}
        return record

    def dataset(record):
        # Old WA samples are unindexed and contain dialogue; retire that
        # collection from training rather than pretending to identify an owner.
        if str(record.get('source', '')).startswith('whatsapp'):
            return None
        return record

    _rewrite(outbox_path, outbox)
    _rewrite(trace_path, trace)
    _rewrite(dataset_path, dataset)
    return True


async def finish_channel_cleanup(pool, key, *, outbox_path):
    import asyncio
    from config import DATASET_PATH
    from trace_llm import _path as trace_path
    await asyncio.to_thread(purge_channel_files, subject_key=key,
        outbox_path=outbox_path, trace_path=trace_path(), dataset_path=DATASET_PATH)
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM whatsapp_channel_cleanup WHERE phone_hash=$1', key)


async def retry_channel_cleanup(pool, *, outbox_path):
    async with pool.acquire() as conn:
        jobs = await conn.fetch('SELECT phone_hash FROM whatsapp_channel_cleanup ORDER BY created_at LIMIT 20')
    for job in jobs:
        try:
            await finish_channel_cleanup(pool, job['phone_hash'], outbox_path=outbox_path)
        except Exception:
            print('[WhatsApp Privacy] Expurgo de arquivos pendente')
