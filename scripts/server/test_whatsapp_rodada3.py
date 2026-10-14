"""Regressões reproduzíveis da rodada 3, sem mensagens ou dados reais."""
import unittest
from unittest.mock import AsyncMock, patch

from orb_router import detect_all, whatsapp_next, route_chat
from conversation_policy import build_conversation_system
from services.family_target import resolve_target_context, active_relations_reply
from services.operations import OperationContext
import test_whatsapp_rodada2 as rodada2
from routers import whatsapp as wa


class ParsingTests(unittest.TestCase):
    def test_192_is_a_glucose_value_not_emergency(self):
        for text in ('Minha glicemia deu 192', 'glicemia 192 mg/dL'):
            result = whatsapp_next(text, True)
            self.assertEqual(result['type'], 'vitals')
            self.assertEqual(result['metric'], 'GLICEMIA')
            self.assertEqual(result['values'], {'glicemia': 192})
        for text in ('ligue 192', 'disque 192', 'preciso de socorro 192', 'dor no peito'):
            self.assertEqual(detect_all(text)['kind'], 'emergency')
        self.assertNotEqual(detect_all('código 1192')['kind'], 'emergency')

    def test_dates_do_not_create_pressure_pending(self):
        for date in ('20/09/2026', '20-09-2026', '2026/09/20'):
            text = 'meu exame de sangue saiu em ' + date
            self.assertEqual(detect_all(text)['kind'], 'exams')
            self.assertEqual(whatsapp_next(text, True)['type'], 'text')
        self.assertEqual(detect_all('pressão 120/80 em 20/09/2026')['sys'], 120)
        self.assertEqual(detect_all('pressão 12/8')['dia'], 80)

    def test_missing_quantity_is_not_invented(self):
        for text in ('Bebi água', 'Tomei água'):
            self.assertEqual(detect_all(text)['kind'], 'hydration_missing')
            result = whatsapp_next(text, True)
            self.assertEqual(result['type'], 'text')
            self.assertNotIn('250', result['fala'])
        self.assertEqual(whatsapp_next('bebi 1 litro e meio', True)['values']['quantidade_ml'], 1500)
        self.assertEqual(whatsapp_next('bebi 1,5 litros', True)['values']['quantidade_ml'], 1500)

    def test_two_metrics_are_not_silently_discarded(self):
        for text in ('Minha pressão 120 por 80 e glicemia 95', 'batimentos 99 e oxigenação 98',
                     'pesei 78 kg e bebi 300 ml de água'):
            result = whatsapp_next(text, True)
            self.assertEqual(result['type'], 'text')
            self.assertIn('separada', result['fala'])
        self.assertEqual(whatsapp_next('pressão 120/80', True)['type'], 'vitals')

    def test_weight_past_tense_is_recordable(self):
        result = whatsapp_next('pesei 78,5 kg hoje de manhã', True)
        self.assertEqual(result['metric'], 'PESO')
        self.assertEqual(result['values']['peso_kg'], 78.5)

    def test_invalid_glucose_is_validated_without_llm(self):
        result = whatsapp_next('glicemia 15', True)
        self.assertEqual(result['type'], 'text')
        self.assertIn('Valor inválido', result['fala'])

    def test_abdominal_pain_and_vomiting_have_relevant_replies(self):
        result = whatsapp_next('A dor não é no peito, é na barriga forte desde ontem', True)
        self.assertEqual(result['type'], 'text')
        self.assertIn('barriga', result['fala'])
        self.assertNotIn('192', result['fala'])
        self.assertIn('vomitou', route_chat('vomitei 2 vezes')['fala'])
        self.assertNotIn('diarreia', route_chat('vomitei 2 vezes')['fala'])

    def test_medication_question_is_not_a_take(self):
        self.assertEqual(detect_all('Tomei 3 comprimidos de dipirona, posso tomar mais um?')['kind'], 'meds')
        self.assertEqual(detect_all('Tomei meu remédio das 8')['kind'], 'med_take')

    def test_habit_question_and_account_deletion_are_not_measurements(self):
        self.assertEqual(detect_all('Bebo 2 litros por dia, isso é o certo?')['kind'], 'topic')
        result = whatsapp_next('Quero apagar meus dados do servidor, pode apagar agora?', True)
        self.assertEqual(result['type'], 'text')
        self.assertIn('exclusão de conta', result['fala'])

    def test_language_and_password_questions_do_not_reach_generic_bot_reply(self):
        self.assertIn('português', whatsapp_next('Responde em inglês agora, please', True)['fala'])
        result = whatsapp_next('Você tem acesso à minha senha?', True)
        self.assertIn('senha', result['fala'])
        self.assertNotIn('sentimentos', result['fala'])

    def test_policy_requires_portuguese_and_explicit_short_length(self):
        prompt = build_conversation_system('whatsapp', {'response_length': 'short'})
        self.assertIn('sempre em português', prompt)
        self.assertIn('uma ou duas frases', prompt)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rodada2.PipelineTests.asyncSetUp
    incoming = rodada2.PipelineTests.incoming

    async def test_ordinary_words_do_not_target_family(self):
        self.conn.fetch.return_value = [{'patient_id': 'other', 'full_name': 'Bruno Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        ctx = OperationContext(actor_id='actor', patient_id='actor')
        for text in ('Tomei 3 comprimidos de dipirona, posso tomar mais um?',
                     'Bebo 2 litros por dia, isso é o certo?',
                     'Quero apagar meus dados do servidor, pode apagar agora?',
                     'A dor não é no peito, é na barriga forte desde ontem'):
            target, error = await resolve_target_context(self.conn, ctx, text)
            self.assertIsNone(error, text)
            self.assertEqual(target.patient_id, 'actor', text)
        for text in ('Qual a pressão da mãe?', 'Qual a pressão da Maria?', 'qual a pressão da maria?'):
            target, error = await resolve_target_context(self.conn, ctx, text)
            self.assertIsNone(target)
            self.assertIn('Bruno', error.message)
        target, error = await resolve_target_context(self.conn, ctx, 'qual a pressão do bruno?')
        self.assertIsNone(error)
        self.assertEqual(target.patient_id, 'other')

    async def test_list_uses_active_relations_and_reports_database_failure(self):
        self.conn.fetch.return_value = [{'patient_id': 'other', 'full_name': 'Bruno Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        await self.incoming('Quem mais eu acompanho por aqui?')
        self.assertIn('Bruno', self.send.await_args.args[1])
        self.assertIn("status = 'ACTIVE'", self.conn.fetch.await_args.args[0])
        self.conn.fetch.side_effect = RuntimeError('offline')
        result = await active_relations_reply(self.conn, OperationContext(actor_id='actor', patient_id='actor'))
        self.assertEqual(result.status, 'unavailable')
        self.assertNotIn('ninguém', result.message)

    async def test_repro_failures_and_incomplete_readings_never_create_buttons(self):
        with patch.object(wa, 'send_confirmation_buttons', AsyncMock()) as buttons, \
             patch.object(wa, 'create_pending_confirmation', AsyncMock()) as create:
            for text in ('glicemia 15', 'Bebi água', 'pressão 120/80 e glicemia 95',
                         'meu exame de sangue saiu em 20/09/2026',
                         'A dor não é no peito, é na barriga forte desde ontem'):
                await self.incoming(text)
                self.assertNotIn('Não consegui preparar', self.send.await_args.args[1])
        buttons.assert_not_awaited()
        create.assert_not_awaited()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_reasoning_retries_once_without_executing_actions(self):
        from unittest.mock import MagicMock
        from whatsapp_conversation import retry_public_reply, FALLBACK_REPLY
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content':
            '{"fala":"Resposta curta.","acao":"REGISTRAR_AGUA","parametros":{"ml":250}}'}}]}
        client.post = AsyncMock(return_value=response)
        payload = {'messages': [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'pergunta'}], 'max_tokens': 200}
        result = await retry_public_reply(client, 'https://provider.invalid', {}, payload)
        self.assertEqual(result, ('Resposta curta.', 'CONVERSA', {}))
        client.post.assert_awaited_once()
        self.assertEqual(payload['max_tokens'], 200)
        self.assertEqual(payload['messages'][0]['content'], 'policy')
        client.post.side_effect = RuntimeError('offline')
        self.assertEqual(await retry_public_reply(client, 'https://provider.invalid', {}, payload), (FALLBACK_REPLY, 'CONVERSA', {}))


class LLMPreferencePipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rodada2.PipelineTests.asyncSetUp
    incoming = rodada2.PipelineTests.incoming

    async def test_short_preference_reaches_actual_provider_payload(self):
        import asyncio
        from unittest.mock import MagicMock
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': 'Não sei o tempo na sua região.'}}]}
        client.post = AsyncMock(return_value=response)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        with patch.object(wa._state, 'sem_wa', asyncio.Semaphore(1)), \
             patch.object(wa.httpx, 'AsyncClient', factory), \
             patch('services.wa_preferences.read_preferences', AsyncMock(return_value={'response_length': 'short'})):
            await self.incoming('Me conta como está o tempo lá fora')
        sent = client.post.await_args.kwargs['json']
        self.assertIn('uma ou duas frases', sent['messages'][0]['content'])
        self.assertEqual(sent['max_tokens'], 200)
        self.assertIn('tempo', self.send.await_args.args[1])

    async def test_empty_reasoning_is_retried_in_real_whatsapp_pipeline(self):
        import asyncio
        from unittest.mock import MagicMock
        client = MagicMock()
        incomplete = MagicMock(status_code=200)
        incomplete.json.return_value = {'choices': [{'message': {'content': '<think>unfinished'}}]}
        complete = MagicMock(status_code=200)
        complete.json.return_value = {'choices': [{'message': {'content': 'Resposta pública.'}}]}
        client.post = AsyncMock(side_effect=[incomplete, complete])
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        with patch.object(wa._state, 'sem_wa', asyncio.Semaphore(1)), \
             patch.object(wa.httpx, 'AsyncClient', factory), \
             patch('services.wa_preferences.read_preferences', AsyncMock(return_value={})):
            await self.incoming('Me conta como está o tempo lá fora')
        self.assertEqual(client.post.await_count, 2)
        self.assertIn('Resposta pública', self.send.await_args.args[1])
        self.assertNotIn('think', self.send.await_args.args[1])
