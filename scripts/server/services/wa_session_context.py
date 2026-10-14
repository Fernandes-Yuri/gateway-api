"""Contexto recente apenas em RAM; isolado por vínculo, sem preferências."""
from collections import OrderedDict
from contextvars import ContextVar
from time import monotonic

TTL_SECONDS = 1800
MAX_SESSIONS = 1000
MAX_MESSAGES = 12
MAX_CHARS = 12000
_current = ContextVar('wa_dialogue_scope', default=None)
_sessions = OrderedDict()


def scope(key):
    return _current.set(key)


def reset(token):
    _current.reset(token)


def clear(phone_hash):
    for key in list(_sessions):
        if key[0] == phone_hash:
            del _sessions[key]


def _recent(key):
    now = monotonic()
    for expired in [k for k, (seen, _) in _sessions.items() if now - seen >= TTL_SECONDS]:
        del _sessions[expired]
    return _sessions.get(key, (now, []))[1]


def record(role, content, *, phone_hash=None):
    key = _current.get()
    if not key or (phone_hash and key[0] != phone_hash) or role not in ('user', 'assistant') or not content:
        return
    messages = list(_recent(key))
    messages.append({'role': role, 'content': str(content)[:MAX_CHARS]})
    messages = messages[-MAX_MESSAGES:]
    while len(messages) > 1 and sum(len(m['content']) for m in messages) > MAX_CHARS:
        messages.pop(0)
    _sessions[key] = (monotonic(), messages)
    _sessions.move_to_end(key)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def messages(system, current_text):
    key = _current.get()
    recent = list(_recent(key)) if key else []
    # Current text was recorded after identity; never duplicate it.
    if not recent or recent[-1] != {'role': 'user', 'content': current_text[:MAX_CHARS]}:
        recent.append({'role': 'user', 'content': current_text[:MAX_CHARS]})
    recent = recent[-MAX_MESSAGES:]
    while len(recent) > 1 and sum(len(m['content']) for m in recent) > MAX_CHARS:
        recent.pop(0)
    return [{'role': 'system', 'content': system}, *recent]
