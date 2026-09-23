"""Explicit smoke check: synthetic prompts and connection-local temporary tables only."""
import asyncio
import os
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import httpx
from assistant_preferences import PreferenceStore, interpret, process_preferences


class SingleConnection:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return self
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *args): return False


async def main():
    # Never print configuration values or include them in failures.
    for line in Path('/etc/braga-saude/gateway.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            parsed = shlex.split(value)
            os.environ.setdefault(key.strip(), parsed[0] if parsed else '')
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        if '--schema-only' in sys.argv:
            for table in ('assistant_preferences', 'assistant_preference_pending'):
                assert await conn.fetchval('SELECT to_regclass($1)::text', 'public.' + table), 'Missing preference table'
            print('Persistent preference tables present in deployed database.')
            return
        await conn.execute("""CREATE TEMP TABLE assistant_preferences (
            user_id TEXT PRIMARY KEY, preferences JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TEMP TABLE assistant_preference_pending (
            user_id TEXT PRIMARY KEY, proposal_id TEXT, operation JSONB, question TEXT,
            expires_at TIMESTAMPTZ DEFAULT NOW()+INTERVAL '2 minutes');""")
        pool = SingleConnection(conn)
        store = PreferenceStore(pool)
        change = {'op': 'propose', 'key': 'cup_ml', 'value': 300}
        _ = await store.execute('test-a', change, None)
        prefs, pending = await store.read('test-a')
        assert prefs == {}, 'Proposal prematurely persisted'
        await store.execute('test-b', {'op': 'confirm', 'pending_id': pending['proposal_id']}, pending)
        assert (await store.read('test-b'))[0] == {}, 'Account isolation failed'
        await store.execute('test-a', {'op': 'confirm', 'pending_id': pending['proposal_id']}, pending)
        assert (await store.read('test-a'))[0] == {'cup_ml': 300}
        replay = await store.execute('test-a', {'op': 'confirm', 'pending_id': pending['proposal_id']}, pending)
        assert 'já foi resolvida' in replay
        await store.execute('test-a', {'op': 'propose', 'key': 'all', 'value': None}, None)
        _, deletion = await store.read('test-a')
        await store.execute('test-a', {'op': 'confirm', 'pending_id': deletion['proposal_id']}, deletion)
        assert (await store.read('test-a'))[0] == {}
        await store.execute('test-a', change, None)
        _, expired = await store.read('test-a')
        await conn.execute("UPDATE assistant_preference_pending SET expires_at=NOW()-INTERVAL '1 second'")
        await store.execute('test-a', {'op': 'confirm', 'pending_id': expired['proposal_id']}, expired)
        assert (await store.read('test-a'))[0] == {}
        print('PostgreSQL: proposal, confirmation, isolation, replay, deletion and expiration OK')
        url = 'https://api.groq.com/openai/v1/chat/completions'
        token = os.getenv('GROQ_API_KEY_CHAT') or os.getenv('GROQ_API_KEY') or os.getenv('HF_TOKEN')
        model = os.getenv('GROQ_MODEL') or os.getenv('MODEL_ID')
        # Use the same configured model selection as server.py.
        import server
        model = server.MODEL_ID
        cases = [
            ('Pode me chamar de Léo', 'propose', 'nickname', 'Léo'),
            ('Guarde que meu copo habitual tem trezentos mililitros', 'propose', 'cup_ml', 300),
            ('De agora em diante prefiro que você seja mais breve', 'propose', 'response_length', 'short'),
            ('O que você tem guardado sobre mim?', 'list', None, None),
            ('Esqueça o tamanho do meu copo', 'propose', 'cup_ml', None),
            ('Bebi dois copos de água', 'none', None, None),
            ('Meu copo tem 300 ml', 'none', None, None),
            ('Minha irmã prefere respostas curtas', 'none', None, None),
            ('Não guarde que meu copo tem 300 ml', 'none', None, None),
        ]
        async with httpx.AsyncClient(timeout=25) as client:
            for index, (text, op, key, value) in enumerate(cases, 1):
                start = time.monotonic()
                result = await interpret(client, url, token, model, text, None)
                assert result['op'] == op, f'Interpretation case {index}: {result}'
                if key:
                    assert result.get('key') == key and result.get('value') == value, f'Value case {index}: {result}'
                print(f'Model case {index}: OK ({time.monotonic()-start:.2f}s)')
        messages = []
        for text in ['Guarde que meu copo habitual tem 300 ml', 'Sim, pode guardar', 'O que você lembra sobre mim?']:
            messages.append(SimpleNamespace(role='user', content=text))
            reply, prefs = await process_preferences(pool, 'test-c', text, messages, url=url, token=token, model=model)
            assert reply and 'Não consegui' not in reply, 'Conversation executor unavailable'
            messages.append(SimpleNamespace(role='assistant', content=reply))
        assert prefs == {'cup_ml': 300}, 'Conversation did not persist confirmed preference'
        print('Real model + PostgreSQL multi-turn conversation: OK')
    finally:
        await conn.close()  # PostgreSQL drops both temporary tables with the connection.


if __name__ == '__main__':
    asyncio.run(main())
