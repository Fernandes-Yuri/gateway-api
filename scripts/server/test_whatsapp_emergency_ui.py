"""Cartão nativo de URL, página acessível e fallback sem contato real com Meta."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import whatsapp as wa
from services.emergency_reply import SAMU_URL, UPA_URL
from services.emergency_page import emergency_page
from orb_router import detect_all, whatsapp_next
import whatsapp_service as transport
import test_whatsapp_rodada2 as rodada2


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_cta_opens_page_without_raw_urls_in_body(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=200))
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        with patch.object(transport.httpx, 'AsyncClient', factory), \
             patch('config.PUBLIC_API_BASE_URL', 'https://api.example.invalid'), \
             patch.object(transport, 'WHATSAPP_TOKEN', 'synthetic'), \
             patch.object(transport, 'send_whatsapp_text', AsyncMock()) as fallback:
            result = await transport.send_whatsapp_emergency('11999999999')
        self.assertTrue(result['success'])
        payload = client.post.await_args.kwargs['json']
        self.assertEqual(payload['type'], 'interactive')
        self.assertEqual(payload['interactive']['type'], 'cta_url')
        params = payload['interactive']['action']['parameters']
        self.assertEqual(params, {'display_text': 'Opções de socorro', 'url': 'https://api.example.invalid/socorro'})
        self.assertIn('192', payload['interactive']['body']['text'])
        self.assertNotIn('tel:', payload['interactive']['body']['text'])
        self.assertNotIn('https:', payload['interactive']['body']['text'])
        fallback.assert_not_awaited()

    async def test_rejected_cta_preserves_textual_help(self):
        fallback = AsyncMock(return_value={'success': True})
        for result in ({'success': False, 'status_code': 400}, {'success': False, 'status_code': None}):
            fallback.reset_mock()
            with patch.object(transport, 'send_whatsapp_cta_url', AsyncMock(return_value=result)):
                await transport.send_whatsapp_emergency('synthetic', send_text=fallback)
            message = fallback.await_args.args[1]
            self.assertIn(SAMU_URL, message)
            self.assertIn(UPA_URL, message)

    async def test_transient_error_returns_failure_and_never_sends_tel_as_cta_url(self):
        factory = MagicMock()
        factory.return_value.__aenter__.side_effect = httpx.ConnectError('offline')
        with patch.object(transport.httpx, 'AsyncClient', factory):
            result = await transport.send_whatsapp_cta_url('synthetic', 'Ligue 192', 'Opções', 'https://example.invalid/socorro')
        self.assertFalse(result['success'])
        with patch.object(transport.httpx, 'AsyncClient') as client:
            result = await transport.send_whatsapp_cta_url('synthetic', 'Ligue 192', 'Ligar', 'tel:192')
        self.assertFalse(result['success'])
        client.assert_not_called()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rodada2.PipelineTests.asyncSetUp
    incoming = rodada2.PipelineTests.incoming

    async def test_registered_emergency_uses_card_and_no_llm(self):
        with patch.object(wa, 'send_whatsapp_emergency', AsyncMock()) as card, \
             patch.object(wa.httpx, 'AsyncClient') as llm:
            await self.incoming('Estou com dor no peito')
        card.assert_awaited_once()
        self.send.assert_not_awaited()
        llm.assert_not_called()

    async def test_unregistered_emergency_uses_same_card_before_account_gate(self):
        from wa_gate_service import gate_unregistered
        card = AsyncMock()
        result = await gate_unregistered('dor no peito', {'status': 'unlinked'}, self.send, 'synthetic', send_emergency=card)
        self.assertTrue(result)
        card.assert_awaited_once_with('synthetic')
        self.send.assert_not_awaited()

    async def test_rodada4_probes_do_not_enter_family_or_emergency(self):
        self.conn.fetch.return_value = [{'patient_id': 'other', 'full_name': 'Bruno Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        with patch.object(wa, 'send_confirmation_buttons', AsyncMock()) as buttons, \
             patch.object(wa, 'create_pending_confirmation', AsyncMock()) as create, \
             patch.object(wa, 'send_whatsapp_emergency', AsyncMock()) as card:
            await self.incoming('Chegou meu exame de colesterol em 12/08/2026')
            self.assertNotIn('Bruno', self.send.await_args.args[1])
            await self.incoming('Bebi 2 copos de café hoje')
            self.assertNotIn('Bruno', self.send.await_args.args[1])
        buttons.assert_not_awaited()
        create.assert_not_awaited()
        card.assert_not_awaited()
        self.assertEqual(whatsapp_next('Meus batimentos deram 192 agora', True)['values'], {'batimentos': 192})
        self.assertEqual(whatsapp_next('Minha glicemia deu 92', True)['values'], {'glicemia': 92})
        self.assertEqual(detect_all('Qual a minha pressão de hoje?')['kind'], 'query_bp')
