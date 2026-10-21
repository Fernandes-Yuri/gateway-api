"""Persistência transacional e controle humano; nenhuma chamada de LLM aqui."""
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import HTTPException

from services.wa_intake_privacy import phone_hash
from services.wa_intake_retention import expired


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def public_conversation(row):
    keys = ('id', 'mode', 'epoch', 'operator_uid', 'intake_status', 'summary',
            'consented', 'last_inbound_at', 'created_at', 'updated_at')
    result = {k: row[k] for k in keys}
    result['fields'] = decoded(row['fields'])
    result['handoff'] = decoded(row['handoff']) if 'handoff' in row.keys() else None
    return result


def public_message(row):
    keys = ('id', 'conversation_id', 'origin', 'kind', 'body', 'delivery', 'operator_uid',
            'transcript', 'transcript_status', 'error_code', 'created_at')
    result = {k: row[k] for k in keys}
    if 'media_id' in row.keys():
        result['media_id'], result['media_status'] = row['media_id'], row['media_status']
        result['media_mime_type'] = row['media_mime_type']
    return result


async def event(conn, conversation_id, kind, **data):
    # O lock dura até o commit: um cursor nunca pula um evento ainda invisível.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('intake:events',0))")
    return await conn.fetchval(
        'INSERT INTO whatsapp_intake_events(conversation_id,kind,data) VALUES($1,$2,$3::jsonb) RETURNING id',
        conversation_id, kind, json.dumps(data))


@asynccontextmanager
async def locked(pool, conversation_id, *, allow_expired=False):
    async with pool.acquire() as conn, conn.transaction():
        # Envios externos e tomada de controle usam o mesmo lock de linha.
        row = await conn.fetchrow('SELECT * FROM whatsapp_intake_conversations WHERE id=$1 FOR UPDATE', conversation_id)
        if not row:
            raise HTTPException(404, 'Atendimento não encontrado.')
        if not allow_expired and expired(row):
            raise HTTPException(410, 'Prazo deste atendimento encerrado. O histórico está em expurgo.')
        yield conn, row


async def authorized(conn, uid):
    return await conn.fetchval('SELECT enabled FROM whatsapp_intake_operators WHERE uid=$1 FOR SHARE', uid) is True


async def queue_message(conn, row, text, *, origin='bot', key=None, uid=None, kind='text', request_hash=None):
    key = key or 'system-' + uuid.uuid4().hex
    message_id = await conn.fetchval(
        "INSERT INTO whatsapp_intake_messages(conversation_id,origin,kind,body,epoch,idempotency_key,operator_uid,delivery,request_hash) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,'queued',$8) RETURNING id",
        row['id'], origin, kind, text, row['epoch'], key, uid, request_hash)
    return message_id


async def handoff(conn, row, reason):
    if row['mode'] == 'human' and reason == 'emergency':
        await conn.execute("UPDATE whatsapp_intake_conversations SET intake_status='emergency',updated_at=NOW() WHERE id=$1", row['id'])
        await conn.execute('INSERT INTO whatsapp_intake_handoffs(id,conversation_id,epoch,reason) VALUES($1,$2,$3,$4) ON CONFLICT(conversation_id,epoch) DO NOTHING',
                           uuid.uuid4(), row['id'], row['epoch'], reason)
        return row['epoch']
    epoch = await conn.fetchval(
        "UPDATE whatsapp_intake_conversations SET mode='waiting_human',epoch=epoch+1,operator_uid=NULL,"
        "intake_status=$2,updated_at=NOW() WHERE id=$1 RETURNING epoch", row['id'], reason)
    await conn.execute("UPDATE whatsapp_intake_messages SET delivery='cancelled' WHERE conversation_id=$1 AND origin='bot' AND delivery='queued'", row['id'])
    await conn.execute('INSERT INTO whatsapp_intake_handoffs(id,conversation_id,epoch,reason) VALUES($1,$2,$3,$4)',
                       uuid.uuid4(), row['id'], epoch, reason)
    return epoch


async def ingest(pool, body, receiver):
    accepted = 0
    events = []
    async with pool.acquire() as conn, conn.transaction():
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value') or {}
                if value.get('metadata', {}).get('phone_number_id') != receiver:
                    if value.get('messages') or value.get('statuses'):
                        raise HTTPException(403, 'Número receptor inválido.')
                    continue
                for message in value.get('messages', []):
                    phone, meta_id = message.get('from'), message.get('id')
                    if not isinstance(phone, str) or not phone.isdigit() or not 8 <= len(phone) <= 16 or not isinstance(meta_id, str) or len(meta_id) > 512:
                        raise HTTPException(400, 'Mensagem inválida.')
                    key = phone_hash(phone)
                    conversation_id = await conn.fetchval(
                        'INSERT INTO whatsapp_intake_conversations(id,receiver,phone,phone_hash) VALUES($1,$2,$3,$4) '
                        'ON CONFLICT(receiver,phone_hash) DO UPDATE SET receiver=EXCLUDED.receiver RETURNING id',
                        uuid.uuid4(), receiver, phone, key)
                    # Nunca salvar nomes de perfil ou conteúdo fora do mínimo recebido.
                    result = await conn.fetchval(
                        'INSERT INTO whatsapp_intake_inbox(conversation_id,meta_id,payload) VALUES($1,$2,$3::jsonb) '
                        'ON CONFLICT DO NOTHING RETURNING id', conversation_id, meta_id, json.dumps(message))
                    accepted += int(result is not None)
                    if result is not None:
                        try:
                            sent_at = datetime.fromtimestamp(float(message.get('timestamp', datetime.now(timezone.utc).timestamp())), timezone.utc)
                        except (ValueError, TypeError, OverflowError):
                            raise HTTPException(400, 'Timestamp inválido.') from None
                        sent_at = min(sent_at, datetime.now(timezone.utc))
                        await conn.execute('UPDATE whatsapp_intake_conversations SET last_inbound_at=GREATEST(last_inbound_at,$2),updated_at=NOW() WHERE id=$1', conversation_id, sent_at)
                        events.append((conversation_id, 'conversation.updated', {}))
                for status in value.get('statuses', []):
                    stage = status.get('status')
                    if stage not in ('sent', 'delivered', 'read', 'failed'):
                        continue
                    row = await conn.fetchrow(
                        'SELECT c.* FROM whatsapp_intake_conversations c JOIN whatsapp_intake_messages m ON m.conversation_id=c.id '
                        'WHERE m.meta_id=$1 AND c.receiver=$2 FOR UPDATE OF c', status.get('id'), receiver)
                    if not row:
                        continue
                    changed = await conn.fetchval(
                        "UPDATE whatsapp_intake_messages SET delivery=$2 WHERE meta_id=$1 AND "
                        "CASE delivery WHEN 'sending' THEN 0 WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 4 END "
                        "< CASE $2::text WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2 WHEN 'read' THEN 3 ELSE 4 END RETURNING id",
                        status['id'], stage)
                    if changed:
                        events.append((row['id'], 'message.delivery_changed', {'message_id': changed, 'delivery': stage}))
        for conversation_id, kind, data in events:
            await event(conn, conversation_id, kind, **data)
    return accepted


async def control(pool, conversation_id, uid, action, request):
    async with locked(pool, conversation_id) as (conn, row):
        if not await authorized(conn, uid):
            raise HTTPException(403, 'Acesso de operador revogado.')
        old = await conn.fetchrow('SELECT * FROM whatsapp_intake_commands WHERE conversation_id=$1 AND key=$2', conversation_id, request.idempotency_key)
        if old:
            if old['uid'] != uid or old['action'] != action or old['epoch'] != request.epoch + 1:
                raise HTTPException(409, 'Chave de idempotência já usada.')
            return {'conversation_id': conversation_id, 'epoch': old['epoch'], 'mode': {'claim': 'human', 'release': 'bot', 'close': 'closed'}[action]}
        if row['epoch'] != request.epoch:
            raise HTTPException(409, 'Controle mudou. Atualize o atendimento.')
        if row['mode'] == 'closed' or (row['mode'] == 'human' and row['operator_uid'] != uid):
            raise HTTPException(409, 'Atendimento indisponível para esta ação.')
        mode = {'claim': 'human', 'release': 'bot', 'close': 'closed'}[action]
        epoch = row['epoch'] + 1
        await conn.execute('UPDATE whatsapp_intake_conversations SET mode=$2,epoch=$3,operator_uid=$4,updated_at=NOW() WHERE id=$1',
                           conversation_id, mode, epoch, uid if mode == 'human' else None)
        await conn.execute("UPDATE whatsapp_intake_messages SET delivery='cancelled' WHERE conversation_id=$1 AND delivery='queued'", conversation_id)
        await conn.execute('INSERT INTO whatsapp_intake_commands VALUES($1,$2,$3,$4,$5)', conversation_id, request.idempotency_key, uid, action, epoch)
        if action == 'claim':
            await conn.execute("UPDATE whatsapp_intake_handoffs SET status='claimed' WHERE conversation_id=$1 AND status<>'claimed'", conversation_id)
        await event(conn, conversation_id, 'control.changed', mode=mode, epoch=epoch)
        return {'conversation_id': conversation_id, 'epoch': epoch, 'mode': mode}


async def human_message(pool, conversation_id, uid, request, *, content=None, mime=None):
    kind = 'audio' if content is not None else 'text'
    text = '' if content is not None else request.text
    digest = hashlib.sha256(f'{kind}:{mime}:{request.epoch}:'.encode() + (content if content is not None else text.encode())).hexdigest()
    async with locked(pool, conversation_id) as (conn, row):
        if not await authorized(conn, uid):
            raise HTTPException(403, 'Acesso de operador revogado.')
        previous = await conn.fetchrow('SELECT * FROM whatsapp_intake_messages WHERE conversation_id=$1 AND idempotency_key=$2', conversation_id, request.idempotency_key)
        if previous:
            if previous['operator_uid'] != uid or previous['request_hash'] != digest:
                raise HTTPException(409, 'Chave de idempotência já usada.')
            return {'message_id': previous['id'], 'delivery': previous['delivery']}
        if row['mode'] != 'human' or row['operator_uid'] != uid or row['epoch'] != request.epoch:
            raise HTTPException(409, 'Assuma o atendimento antes de responder.')
        if not row['consented']:
            raise HTTPException(409, 'Aguardando aceite do contato.')
        if not row['last_inbound_at'] or (datetime.now(timezone.utc) - row['last_inbound_at']).total_seconds() >= 86400:
            raise HTTPException(409, 'Janela de resposta encerrada; aguarde nova mensagem do contato.')
        mid = await queue_message(conn, row, text, origin='human', key=request.idempotency_key,
                                  uid=uid, kind=kind, request_hash=digest)
        if content is not None:
            await conn.execute("INSERT INTO whatsapp_intake_media(id,message_id,mime_type,content,status) VALUES($1,$2,$3,$4,'ready')", uuid.uuid4(), mid, mime, content)
        await event(conn, conversation_id, 'message.created', message_id=mid)
        return {'message_id': mid, 'delivery': 'queued'}
