"""Rastreio de I/O do LLM (investigação de repetição/delírio).

Cada chamada ao provedor grava um evento JSONL com ID de correlação:
  trace_llm(channel, stage, trace_id=None, **fields) -> trace_id

Canais: chat-ws, chat-rest, whatsapp.
Stages: request, router_hit, followup_hit, classify_request,
  classify_response, speak_request, speak_response, llm_request,
  llm_response, error.

Controles por env: TRACE_LLM=1 (default ligado), TRACE_PATH (default
llm_trace.jsonl no CWD), TRACE_CAP (teto por campo, default 8000).
Nunca quebra o produto: qualquer falha interna é silenciosa.
Nunca grava segredos (só payloads já montados, sem headers).
"""

import json
import os
import threading
import time
import uuid

_lock = threading.Lock()


def _enabled() -> bool:
    return os.getenv("TRACE_LLM", "1") == "1"


def _path() -> str:
    return os.getenv("TRACE_PATH", "llm_trace.jsonl")


def _cap() -> int:
    try:
        return max(50, int(os.getenv("TRACE_CAP", "8000")))
    except ValueError:
        return 8000


def _trim(value, cap: int):
    if isinstance(value, str):
        if len(value) > cap:
            return value[:cap] + f"...[truncado {len(value) - cap} chars]"
        return value
    if isinstance(value, list):
        return [_trim(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: _trim(v, cap) for k, v in value.items()}
    return value


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def trace_llm(channel: str, stage: str, trace_id=None, **fields) -> str:
    """Grava um evento. Devolve o trace_id (cria um se ausente)."""
    tid = trace_id or new_trace_id()
    if not _enabled():
        return tid
    try:
        cap = _cap()
        if channel == 'whatsapp':
            # Diagnostics carry neither dialogue, health values nor prompts.
            fields = {key: value for key, value in fields.items()
                      if key in ('phone_hash', 'kind', 'status', 'type', 'finish_reason', 'context_messages')
                      and isinstance(value, (str, bool, int))}
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "trace_id": tid,
            "channel": channel,
            "stage": stage,
            "fields": _trim(fields, cap),
        }
        line = json.dumps(record, ensure_ascii=False)
        path = _path()
        from services.wa_channel_storage import file_lock
        with _lock, file_lock(path):
            new_file = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if new_file:
                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass
    except Exception:
        pass
    return tid
