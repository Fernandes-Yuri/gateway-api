"""WhatsApp como apoio ao app e respostas públicas completas (rodada 5)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from orb_router import whatsapp_next
from routers import whatsapp as wa
from services.care_intent import is_reminder_request
from whatsapp_conversation import FALLBACK_REPLY, retry_public_reply
import test_whatsapp_rodada2 as rodada2
from regression_verdict import evaluate


class RoutingTests(unittest.TestCase):
    def test_reminders_point_to_app_without_disabling_supported_functions(self):
        for text in ('Me lembra de beber água', 'Me lembre de beber agua',
                     'Me lembra de tomar cafe amanha', 'Me avise a cada 2 horas para beber água',
                     'Quero criar um lembrete de água'):
            with self.subTest(text=text):
                self.assertTrue(is_reminder_request(text))
                result = whatsapp_next(text, True)
                self.assertEqual(result['type'], 'text')
                self.assertIn('aplicativo Braga Saúde', result['fala'])
                self.assertNotIn('enviar agora', result['fala'])
        self.assertFalse(is_reminder_request('O que voce lembra de mim?'))
        self.assertFalse(is_reminder_request('Avise a mãe para beber água'))
        self.assertEqual(whatsapp_next('Minha glicemia deu 192', True)['type'], 'vitals')
        self.assertEqual(whatsapp_next('Estou com dor no peito', True)['acao'], 'EMERGENCIA')

    def test_coffee_report_is_acknowledged_without_asking_to_repeat_it(self):
        result = whatsapp_next('Bebi 2 copos de café hoje', True)
        self.assertIn('você bebeu café', result['fala'])
        self.assertNotIn('Me conta o que foi', result['fala'])
        self.assertNotIn('anotado', result['fala'].lower())

    def test_two_checks_on_same_line_do_not_become_one_greedy_pattern(self):
        result = evaluate({'status': 'ok', 'resposta': 'Resposta correta.',
                           'esperado': 'DEVE: /Resposta/ · NÃO DEVE: /SAMU/'})
        self.assertEqual(result['veredito'], 'inconclusivo')
        self.assertIn('linhas distintas', result['motivos'][0])
        result = evaluate({'status': 'ok', 'resposta': 'Resposta correta.',
                           'checks': [{'pattern': 'Resposta', 'present': True}, {'pattern': 'SAMU', 'present': False}]})
        self.assertEqual(result['veredito'], 'aprovado')


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rodada2.PipelineTests.asyncSetUp
    incoming = rodada2.PipelineTests.incoming

    async def test_personal_reminders_never_create_care_cards_or_resolve_family(self):
        with patch.object(wa, 'send_confirmation_buttons', AsyncMock()) as buttons, \
             patch('services.care_messages.prepare_care_message', AsyncMock()) as prepare, \
             patch.object(wa, '_WA_FAMILY_RESOLVER', AsyncMock()) as family:
            for text in ('Me lembra de beber água', 'Me lembra de tomar cafe amanha',
                         'Me avise a cada 2 horas para beber água'):
                await self.incoming(text)
                self.assertIn('aplicativo Braga Saúde', self.send.await_args.args[1])
        buttons.assert_not_awaited()
        prepare.assert_not_awaited()
        family.assert_not_awaited()

    async def test_explicit_care_send_is_preserved(self):
        from services.operations import OperationResult
        with patch.object(wa, '_WA_FAMILY_RESOLVER', None), \
             patch('services.care_messages.prepare_care_message', AsyncMock(return_value=
                   OperationResult.confirmation('Posso enviar para o familiar?', 'SYNTHETIC', {'body': 'Beba água'}))) as prepare, \
             patch.object(wa, 'send_confirmation_buttons', AsyncMock()) as buttons:
            await self.incoming('Avise a mãe para beber água')
        prepare.assert_awaited_once()
        buttons.assert_awaited_once()

    async def test_provider_truncation_retries_without_sending_partial_text(self):
        partial = 'Não sei o que está no seu plano de'
        client = MagicMock()
        first = MagicMock(status_code=200)
        first.json.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': partial}}]}
        second = MagicMock(status_code=200)
        second.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Não tenho previsão do tempo para a sua região.'}}]}
        client.post = AsyncMock(side_effect=[first, second])
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        with patch.object(wa._state, 'sem_wa', asyncio.Semaphore(1)), \
             patch.object(wa.httpx, 'AsyncClient', factory), \
             patch('services.wa_preferences.read_preferences', AsyncMock(return_value={'response_length': 'short'})):
            await self.incoming('Me conta como está o tempo lá fora')
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(client.post.await_args_list[0].kwargs['json']['max_tokens'], 500)
        self.assertNotIn('\"response_length\": \"short\"', client.post.await_args.kwargs['json']['messages'][0]['content'])
        self.assertNotIn(partial, self.send.await_args.args[1])
        self.assertIn('região.', self.send.await_args.args[1])

    async def test_retry_that_is_also_truncated_returns_complete_fallback_without_actions(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content':
            '{"fala":"uma frase que corta","acao":"REGISTRAR_AGUA","parametros":{"ml":250}}'}}]}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        result = await retry_public_reply(client, 'https://provider.invalid', {},
            {'messages': [{'role': 'system', 'content': 'Responda curto.'}], 'max_tokens': 500})
        self.assertEqual(result, (FALLBACK_REPLY, 'CONVERSA', {}))
        client.post.assert_awaited_once()
