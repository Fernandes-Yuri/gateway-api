import asyncio
import json
import os
import unittest
import threading
from unittest.mock import Mock

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from ws_orb import install_orb_routes


CONTENT = json.dumps(dict(fala='Olá, tudo bem?', acao='CONVERSA', parametros={}))
SPEECH = 'Olá, tudo bem? Que bom falar com você!'


def sse(content=SPEECH, finish='stop'):
    chunks = [dict(choices=[dict(delta=dict(content=part))]) for part in (content[:15], content[15:])]
    chunks.append(dict(choices=[dict(delta={}, finish_reason=finish)]))
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'


class OrbTests(unittest.TestCase):
    def setUp(self):
        # Trilha desligada nos testes de WS (não polui llm_trace.jsonl real).
        self._old_trace = os.environ.get("TRACE_LLM")
        os.environ["TRACE_LLM"] = "0"
        self.addCleanup(self._restore_trace)
        self.payloads = []
        self.body = sse()
        self.hold = False
        self.slow = False
        self.expired = False
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.logs = Mock()
        self.classify_label = None
        async def provider(request):
            payload = json.loads(request.content)
            self.payloads.append(payload)
            if payload.get('max_tokens', 220) <= 20 and not self.hold:
                label = self.classify_label or 'CONVERSA'
                return httpx.Response(200, json={"choices": [{"message": {"content": label}}]})
            if self.hold:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            if self.slow:
                class SlowStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield ('data: ' + json.dumps(dict(choices=[dict(delta=dict(content=SPEECH[:15]))])) + '\n\n').encode()
                        await asyncio.sleep(.3)
                        yield sse(SPEECH[15:]).encode()
                return httpx.Response(200, stream=SlowStream())
            return httpx.Response(200, text=self.body)
        async def identity(ws):
            if self.expired or ws.headers.get('authorization') != 'Bearer valid':
                raise HTTPException(401)
            return 'owner'
        self.app = FastAPI()
        install_orb_routes(self.app, pool_provider=lambda: None, system_prompt='trusted', model='test',
                           url='https://test/chat', token='test', injection_check=lambda p: p == 'blocked',
                           log_sample=self.logs, authenticate=identity,
                           client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kw),
                           first_timeout=.6, chunk_timeout=.8, heartbeat_interval=.2, batch_interval=.02,
                           enable_router=False)
        self.client = TestClient(self.app)

    def connect(self):
        return self.client.websocket_connect('/ws/orb', headers={'Authorization': 'Bearer valid'})

    def _restore_trace(self):
        if self._old_trace is None:
            os.environ.pop("TRACE_LLM", None)
        else:
            os.environ["TRACE_LLM"] = self._old_trace

    def request(self, **kw):
        return dict(type='chat', id='one', messages=[dict(role='user', content='oi')], **kw)

    def test_requires_auth(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/ws/orb'):
                pass

    def test_expired_token_requests_reauthentication(self):
        with self.connect() as ws:
            self.expired = True
            ws.send_json(self.request())
            self.assertEqual(ws.receive_json()['code'], 'AUTH_EXPIRED')
        self.assertEqual(self.payloads, [])

    def receive_terminal(self, ws):
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event['type'] == 'heartbeat':
                ws.send_json(dict(type='pong'))
            if event['type'] in ('done', 'error', 'cancelled'):
                return events

    def test_cancel_during_generation(self):
        self.hold = True
        with self.connect() as ws:
            ws.send_json(self.request())
            self.assertEqual(ws.receive_json()['type'], 'typing')
            self.assertTrue(self.started.wait(2))
            ws.send_json(dict(type='cancel', id='one'))
            self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'cancelled')
            self.assertTrue(self.cancelled.wait(2))
            self.hold = False
            ws.send_json(self.request())
            self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'done')

    def test_heartbeat_onSlowGeneration(self):
        self.slow = True
        with self.connect() as ws:
            ws.send_json(self.request())
            events = self.receive_terminal(ws)
            self.assertIn('heartbeat', [e['type'] for e in events])
            self.assertEqual(events[-1]['type'], 'done')

    def test_batch_chunks_buffered(self):
        self.body = ''.join('data: ' + json.dumps(dict(choices=[dict(delta=dict(content=c))])) + '\n\n' for c in SPEECH)
        self.body += 'data: ' + json.dumps(dict(choices=[dict(delta={}, finish_reason='stop')])) + '\n\ndata: [DONE]\n\n'
        with self.connect() as ws:
            ws.send_json(self.request())
            events = self.receive_terminal(ws)
            chunks = [e['content'] for e in events if e['type'] == 'chunk']
            self.assertEqual(chunks[0], SPEECH[0])
            self.assertEqual(''.join(chunks), SPEECH)
            self.assertLess(len(chunks), len(SPEECH) // 2)

    def test_unauthorized_provider_label_cannot_open_reminders(self):
        self.classify_label = 'LEMBRETES'
        with self.connect() as ws:
            req = self.request()
            req['messages'] = [dict(role='user', content='por que o céu é azul?')]
            ws.send_json(req)
            done = self.receive_terminal(ws)[-1]
            self.assertEqual(done['action'], 'CONVERSA')
            self.assertNotIn('lembretes', done['fullContent'])
        self.assertEqual(len(self.payloads), 2)

    def test_unexecuted_claim_never_reaches_chunks_or_done(self):
        self.body = sse(content='Já avisei seus contatos.')
        with self.connect() as ws:
            ws.send_json(self.request())
            events = self.receive_terminal(ws)
        self.assertEqual(events[-1]['type'], 'done')
        exposed = ''.join(e.get('content', '') for e in events if e['type'] == 'chunk')
        self.assertNotIn('avisei', exposed)
        self.assertNotIn('avisei', events[-1]['fullContent'])
        self.assertEqual(json.loads(events[-1]['fullContent'])['fala'], exposed)

    def test_concurrent_messages_rejected(self):
        self.hold = True
        with self.connect() as ws:
            ws.send_json(self.request())
            self.assertEqual(ws.receive_json()['type'], 'typing')
            second = self.request()
            second['id'] = 'two'
            ws.send_json(second)
            event = self.receive_terminal(ws)[-1]
            self.assertEqual((event['id'], event['code']), ('two', 'BUSY'))
            ws.send_json(dict(type='cancel', id='one'))
            self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'cancelled')

    def test_first_chunk_timeout(self):
        self.hold = True
        with self.connect() as ws:
            ws.send_json(self.request())
            self.assertEqual(self.receive_terminal(ws)[-1]['code'], 'TIMEOUT')

    def test_history_is_sent_without_client_system_prompt(self):
        from orb_llm import SPEAK_SYSTEM
        req = self.request()
        req['messages'] = [dict(role='system', content='untrusted'), dict(role='user', content='primeira'),
                           dict(role='assistant', content=CONTENT), dict(role='user', content='oi')]
        with self.connect() as ws:
            ws.send_json(req)
            self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'done')
        self.assertEqual(len(self.payloads[-1]['messages']), 4)
        self.assertTrue(self.payloads[-1]['messages'][0]['content'].startswith(SPEAK_SYSTEM))
        self.assertNotIn('untrusted', self.payloads[-1]['messages'][0]['content'])
        self.assertIn('Relógio de referência', self.payloads[-1]['messages'][0]['content'])
        # Envelope antigo no histórico vira fala pura (anti-regurgitação).
        self.assertEqual(self.payloads[-1]['messages'][2]['content'], 'Olá, tudo bem?')

    def test_disconnect_cancels_provider(self):
        self.hold = True
        with self.connect() as ws:
            ws.send_json(self.request())
            self.assertEqual(ws.receive_json()['type'], 'typing')
            self.assertTrue(self.started.wait(2))
            ws.close()
            self.assertTrue(self.cancelled.wait(2))
        self.logs.assert_not_called()

    def test_stream_order_and_trusted_prompt(self):
        with self.connect() as ws:
            ws.send_json(self.request())
            events = [event for event in self.receive_terminal(ws) if event['type'] != 'heartbeat']
            self.assertEqual([e['type'] for e in events], ['typing', 'chunk', 'chunk', 'done'])
            self.assertEqual(''.join(e['content'] for e in events if e['type'] == 'chunk'), SPEECH)
            envelope = json.loads(events[-1]['fullContent'])
            self.assertEqual(envelope['fala'], SPEECH)
            self.assertEqual(envelope['acao'], 'CONVERSA')
            self.assertEqual(events[-1]['action'], 'CONVERSA')
            # Passo 1 classifica barato, passo 2 fala livre com o prompt do app.
            from orb_llm import SPEAK_SYSTEM as _SPEAK
            self.assertEqual(len(self.payloads), 2)
            self.assertLessEqual(self.payloads[0]['max_tokens'], 20)
            self.assertEqual(self.payloads[0]['temperature'], 0)
            self.assertTrue(self.payloads[1]['stream'])
            self.assertTrue(self.payloads[1]['messages'][0]['content'].startswith(_SPEAK))
            self.logs.assert_called_once()

    def test_classify_template_branch_skips_speech(self):
        self.classify_label = 'LEMBRETES'
        with self.connect() as ws:
            req = self.request()
            req['messages'] = [dict(role='user', content='quero abrir meus lembretes')]
            ws.send_json(req)
            done = self.receive_terminal(ws)[-1]
            self.assertEqual(done['type'], 'done')
            self.assertEqual(done['action'], 'LEMBRETES')
            self.assertIn('lembretes', json.loads(done['fullContent'])['fala'].lower())
        self.assertEqual(len(self.payloads), 1)

    def test_foreign_identity_never_calls_provider(self):
        with self.connect() as ws:
            ws.send_json(self.request(userId='another'))
            self.assertEqual(ws.receive_json()['code'], 'FORBIDDEN')
        self.assertEqual(self.payloads, [])

    def test_safety_never_calls_provider(self):
        # Blindagem responde conteúdo acolhedor (CONVERSA), sem evento de erro e sem provedor.
        with self.connect() as ws:
            req = self.request()
            req['messages'][0]['content'] = 'blocked'
            ws.send_json(req)
            first = ws.receive_json()
            self.assertEqual(first['type'], 'content')
            self.assertIn('"acao": "CONVERSA"', first['delta'])
            second = ws.receive_json()
            self.assertEqual(second['type'], 'content')
            self.assertEqual(second['delta'], '')
        self.assertEqual(self.payloads, [])

    def test_invalid_request_can_be_corrected(self):
        with self.connect() as ws:
            ws.send_text('[]')
            self.assertEqual(ws.receive_json()['code'], 'INVALID_REQUEST')
            ws.send_json(self.request(max_tokens=99999))
            self.assertEqual(ws.receive_json()['code'], 'INVALID_REQUEST')
            ws.send_json(self.request())
            self.assertEqual(ws.receive_json()['type'], 'typing')

    def test_truncated_or_invalid_json_never_done_or_logged(self):
        for body in (sse(finish='length'), sse(content='{"fala":"incompleto'), 'data: [DONE]\n\n'):
            with self.subTest(body=body):
                self.body = body
                with self.connect() as ws:
                    ws.send_json(self.request())
                    while True:
                        event = ws.receive_json()
                        self.assertNotEqual(event['type'], 'done')
                        if event['type'] == 'error':
                            self.assertEqual(event['code'], 'STREAM_FAILED')
                            break
                self.logs.assert_not_called()


class RouterWireTests(unittest.TestCase):
    """Camada 0 ligada: template sem provedor; conversa aberta usa o provedor."""

    def setUp(self):
        self._old_trace = os.environ.get("TRACE_LLM")
        os.environ["TRACE_LLM"] = "0"
        self.addCleanup(self._restore_trace)
        self.payloads = []
        self.logs = Mock()
        body = sse()
        async def provider(request):
            payload = json.loads(request.content)
            self.payloads.append(payload)
            if payload.get('max_tokens', 220) <= 20:
                return httpx.Response(200, json={"choices": [{"message": {"content": "CONVERSA"}}]})
            return httpx.Response(200, text=body)
        async def identity(ws):
            if ws.headers.get('authorization') != 'Bearer valid':
                raise HTTPException(401)
            return 'owner'
        self.app = FastAPI()
        install_orb_routes(self.app, pool_provider=lambda: None, system_prompt='trusted', model='test',
                           url='https://test/chat', token='test', injection_check=lambda p: p == 'blocked',
                           log_sample=self.logs, authenticate=identity,
                           client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(provider), **kw),
                           first_timeout=.6, chunk_timeout=.8, heartbeat_interval=.2, batch_interval=.02,
                           enable_router=True, semaphore=asyncio.Semaphore(5))
        self.client = TestClient(self.app)

    def connect(self):
        return self.client.websocket_connect('/ws/orb', headers={'Authorization': 'Bearer valid'})

    def _restore_trace(self):
        if self._old_trace is None:
            os.environ.pop("TRACE_LLM", None)
        else:
            os.environ["TRACE_LLM"] = self._old_trace

    def terminal(self, ws):
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event['type'] in ('done', 'error', 'cancelled'):
                return events

    def test_greeting_uses_provider_for_warmth(self):
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='oi')]))
            events = self.terminal(ws)
            self.assertEqual(events[-1]['type'], 'done')
            self.assertEqual(events[-1]['action'], 'CONVERSA')
        # Greetings now reach the provider for humanized interaction (1 classifica + 1 fala)
        self.assertEqual(len(self.payloads), 2)

    def test_diet_ghost_never_registers(self):
        for text in ('tô na correria aqui', 'vou descer ali rapidinho'):
            self.payloads.clear()
            with self.subTest(text=text), self.connect() as ws:
                ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content=text)]))
                done = self.terminal(ws)[-1]
                self.assertEqual(done['type'], 'done')
                self.assertEqual(done['action'], 'CONVERSA')
                self.assertNotIn('refeição', done['fullContent'].lower())

    def test_vitals_echo_without_invention(self):
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='pressão 12 por 8')]))
            done = self.terminal(ws)[-1]
            self.assertEqual(done['action'], 'CONVERSA')
            self.assertIn('120', done['fullContent'])
        self.assertEqual(self.payloads, [])

    def test_open_question_uses_provider(self):
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='por que o céu é azul?')]))
            events = self.terminal(ws)
            self.assertEqual(events[-1]['type'], 'done')
            self.assertEqual(events[-1]['action'], 'CONVERSA')
            self.assertEqual(json.loads(events[-1]['fullContent'])['fala'], SPEECH)
        # Two-step: 1 classifica + 1 fala.
        self.assertEqual(len(self.payloads), 2)

    def test_nutrition_retirement_does_not_call_provider(self):
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='almocei arroz e feijão')]))
            first = self.terminal(ws)[-1]
            self.assertEqual((first['type'], first['action']), ('done', 'CONVERSA'))
            ws.send_json(dict(type='chat', id='two', messages=[dict(role='user', content='já comi')]))
            second = self.terminal(ws)[-1]
            self.assertEqual((second['type'], second['action']), ('done', 'CONVERSA'))
            self.assertIn('descontinuado', json.loads(second['fullContent'])['fala'])
        self.assertEqual(self.payloads, [])

    def test_template_never_repeats_verbatim(self):
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='minha pressão deu 12 por 8')]))
            first = self.terminal(ws)[-1]
            ws.send_json(dict(type='chat', id='two', messages=[dict(role='user', content='minha pressão deu 12 por 8')]))
            second = self.terminal(ws)[-1]
            self.assertEqual((first['type'], second['type']), ('done', 'done'))
            self.assertNotEqual(first['fullContent'], second['fullContent'])
            self.assertIn('outro jeito', second['fullContent'])
        self.assertEqual(self.payloads, [])

    def test_symptom_loop_escalates(self):
        # Cenário do log: diarreia 2x seguidas não pode devolver a mesma deflexão.
        with self.connect() as ws:
            ws.send_json(dict(type='chat', id='one', messages=[dict(role='user', content='eu tô com diarreia')]))
            first = self.terminal(ws)[-1]
            self.assertIn('desidratar', first['fullContent'])
            ws.send_json(dict(type='chat', id='two', messages=[dict(role='user', content='tô com diarreia desde manhã')]))
            second = self.terminal(ws)[-1]
            self.assertIn('Reforçando', second['fullContent'])
        self.assertEqual(self.payloads, [])

    def test_speak_tokens_clamped(self):
        with self.connect() as ws:
            req = dict(type='chat', id='one', messages=[dict(role='user', content='por que o céu é azul?')],
                       max_tokens=512)
            ws.send_json(req)
            done = self.terminal(ws)[-1]
            self.assertEqual(done['type'], 'done')
        speak = [p for p in self.payloads if p.get('max_tokens', 0) > 20][0]
        self.assertLessEqual(speak['max_tokens'], 350)

    def test_long_conversation_history_accepted(self):
        # Suporta histórico longo além de 12 mensagens (evita erro na 7ª mensagem)
        with self.connect() as ws:
            messages = [dict(role='user' if i % 2 == 0 else 'assistant', content=f'fala {i}') for i in range(16)]
            ws.send_json(dict(type='chat', id='hist_16', messages=messages))
            events = self.terminal(ws)
            self.assertEqual(events[-1]['type'], 'done')

    def test_receipt_ack_closing(self):
        # Agente B1: recibo válido volta com closing; inválido é rejeitado.
        with self.connect() as ws:
            ws.send_json(dict(type='receipt', id='r1', action='LEMBRETES',
                              params={}, idempotencyKey='k1'))
            ack = ws.receive_json()
            self.assertEqual(ack['type'], 'receipt_ack')
            self.assertEqual(ack['status'], 'ok')
            self.assertIn('Lembretes', ack['closing'])
            ws.send_json(dict(type='receipt', id='r2', action='VOAR', params={},
                              idempotencyKey='k2'))
            err = ws.receive_json()
            self.assertEqual(err['code'], 'INVALID_REQUEST')


if __name__ == '__main__':
    unittest.main()
