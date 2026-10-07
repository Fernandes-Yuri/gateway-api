"""Contrato semântico REST com provedor simulado, sem serviços externos."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from routers import chat


class RestSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, raw, label='CONVERSA', context=None):
        calls = []
        async def provider(request):
            payload = json.loads(request.content)
            calls.append(payload)
            content = label if payload['max_tokens'] <= 20 else 'Posso explicar essa dúvida.'
            return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
        factory = httpx.AsyncClient
        state = SimpleNamespace(db_pool=None, groq_api_key='test', groq_api_url='https://test/chat',
                                model_rest='test', sem_rest=asyncio.Semaphore(1),
                                assistant_memory=AsyncMock(return_value=(None, {})),
                                log_fine_tuning_sample=Mock())
        req = chat.ChatCompletionRequest(messages=[chat.Message(role='user', content=raw)], nlu_context=context)
        with patch.object(chat, 'firebase_uid', AsyncMock(return_value='owner')), patch.object(chat, '_state', state), patch.object(chat.httpx, 'AsyncClient',
                 side_effect=lambda **kw: factory(transport=httpx.MockTransport(provider), **kw)), patch.dict(os.environ, TRACE_LLM='0'):
            response = await chat.chat_completions(req, SimpleNamespace(headers={'authorization': 'Bearer test'}))
        return json.loads(response['choices'][0]['message']['content']), calls

    async def test_educational_and_negated_emergency_do_not_authorize_label(self):
        for raw in ('o que significa dor no peito?', 'não estou com dor no peito'):
            response, _ = await self.call(raw, 'EMERGENCIA')
            self.assertEqual(response['acao'], 'CONVERSA')
            self.assertNotIn('opções de socorro', response['fala'])

    async def test_current_emergency_has_priority_without_provider(self):
        response, calls = await self.call('registre minha refeição; estou com falta de ar')
        self.assertEqual(response['acao'], 'EMERGENCIA')
        self.assertEqual(calls, [])

    async def test_beverages_are_resolved_before_provider(self):
        for raw in ('não bebi 500 ml de água', 'não anota 500 ml de água', 'bebi 500 ml de suco'):
            response, calls = await self.call(raw)
            self.assertEqual(response['acao'], 'CONVERSA')
            self.assertNotIn('Boa!', response['fala'])
            self.assertEqual(calls, [])

    async def test_contract_applies_to_router_and_model(self):
        raw = 'quero abrir meus exames'
        response, _ = await self.call(raw, 'EXAMES', {'original_text': raw, 'conversation_only': True})
        self.assertEqual(response['acao'], 'CONVERSA')
        self.assertNotIn('Abrindo', response['fala'])
