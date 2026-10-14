"""Regressões da rodada 2 por pipelines reais, com contas e dados sintéticos."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from core.confirmation import format_confirmation_value
from orb_router import detect_all, whatsapp_next, dedupe_fala, route_chat
from routers import whatsapp as wa
from services.calendar_reply import calendar_reply
from services.emergency_reply import SAMU_URL, UPA_URL
from services.family_target import resolve_target_context
from services.operations import OperationContext, OperationResult
from test_wa_parity_regressions import connection, Pool
from test_whatsapp_bateria import envelope


class RenderingTests(unittest.TestCase):
    def test_all_metrics_have_display_without_raw_dict(self):
        values = {'PRESSAO': {'sistolica': 120, 'diastolica': 80}, 'GLICEMIA': {'glicemia': 100},
                  'AGUA': {'quantidade_ml': 500}, 'PESO': {'peso_kg': 85.0},
                  'BATIMENTOS': {'batimentos': 99}, 'OXIGENACAO': {'saturacao': 98},
                  'MED_TOMADA': {'name': 'Teste', 'scheduled_for': '2026-10-13T08:00:00'},
                  'CARE_MESSAGE': {'body': 'Beba água'}, 'PREFERENCE': {}, 'UNKNOWN': {'secret': 'hidden'}}
        for metric, value in values.items():
            with self.subTest(metric=metric):
                icon, display = format_confirmation_value(metric, value)
                self.assertTrue(icon)
                self.assertNotIn('{', display)
                self.assertNotIn('hidden', display)
        self.assertEqual(format_confirmation_value('BATIMENTOS', values['BATIMENTOS'])[1], '99 bpm')
        self.assertEqual(format_confirmation_value('OXIGENACAO', values['OXIGENACAO'])[1], '98%')

    def test_pressure_is_validated_and_shorthand_is_preserved(self):
        for text in ('Minha pressão deu 70 por 35', 'pressão 70x35', 'pressão 70/35'):
            result = whatsapp_next(text, True)
            self.assertEqual(result['type'], 'text')
            self.assertIn('Diastólica: 35', result['fala'])
            self.assertIn('Valor inválido', result['fala'])
        self.assertEqual(whatsapp_next('pressão 12 por 8', True)['values'], {'sistolica': 120, 'diastolica': 80})
        self.assertEqual(whatsapp_next('pressão 12 por 8 e meio', True)['values']['diastolica'], 85)
        self.assertEqual(whatsapp_next('dor no peito e pressão 70 por 35', True)['acao'], 'EMERGENCIA')

    def test_queries_cannot_be_care_commands(self):
        for text in ('O que voce lembra de mim?', 'O que você sabe de mim?', 'Qual meu apelido?',
                     'Quanto de água bebi hoje?', 'Qual foi minha última pressão?'):
            self.assertNotEqual(detect_all(text)['kind'], 'reminder', text)
        for text in ('Me lembra de beber água', 'Me lembra de tomar café amanhã', 'Avise a mãe para beber água'):
            self.assertEqual(detect_all(text)['kind'], 'reminder', text)

    def test_calendar_exact_hour_and_repetition(self):
        now = datetime(2026, 10, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(calendar_reply('', 'time', now=now), 'Agora são 21 horas em ponto.')
        memory = {}
        outputs = [dedupe_fala(memory, 'synthetic', 'base', 'alternativa') for _ in range(4)]
        self.assertEqual(outputs, ['base', 'alternativa', 'base', 'alternativa'])

    def test_emergency_links_are_shared_and_never_claim_dispatch(self):
        for result in (route_chat('dor no peito'), whatsapp_next('dor no peito', True)):
            self.assertEqual(result['acao'], 'EMERGENCIA')
            self.assertIn(SAMU_URL, result['fala'])
            self.assertIn(UPA_URL, result['fala'])
            self.assertNotIn('Já deixei', result['fala'])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = connection()
        self.conn.fetchval.return_value = 'America/Sao_Paulo'
        self.pool = Pool(self.conn)
        self.send = AsyncMock()
        # Business pipeline tests start after the independent consent guard.
        # Privacy/consent is exercised by test_wa_channel_privacy.
        for item in (patch.object(wa, '_channel_privacy_guard', AsyncMock(return_value=False)),
                     patch.object(wa._state, 'db_pool', self.pool),
                     patch.object(wa, 'send_whatsapp_text', self.send),
                     patch.object(wa, 'HAS_WHATSAPP', True), patch.object(wa, 'HAS_WA_LINK', False),
                     patch.object(wa, 'resolve_linked_account', AsyncMock(return_value={
                         'status': 'confirmed', 'user_id': 'actor', 'full_name': 'Ana'})),
                     patch.object(wa, 'mark_whatsapp_message_read', AsyncMock()),
                     patch.object(wa, 'claim_message_receipt', AsyncMock(return_value=True)),
                     patch.object(wa, 'log_fine_tuning_sample'), patch.object(wa, 'trace_llm'),
                     patch.dict(wa._WA_LASTFALA, clear=True), patch.dict(wa._WA_OPERATION_MEMORY, clear=True)):
            item.start()
            self.addCleanup(item.stop)
        # Usar a constante do contrato de identidade, sem suposição do literal.
        self.identity = {'status': wa.STATUS_OK, 'user_id': 'actor', 'full_name': 'Ana'}

    async def incoming(self, text):
        with patch.object(wa, 'resolve_linked_account', AsyncMock(return_value=self.identity)):
            await wa.process_incoming_whatsapp_payload(envelope([
                {'id': 'synthetic', 'from': '5500000000000', 'type': 'text', 'text': {'body': text}}]))

    async def test_unlinked_emergency_keeps_both_links(self):
        from wa_gate_service import gate_unregistered
        blocked = await gate_unregistered('dor no peito', {'status': 'unlinked'}, self.send, 'synthetic')
        self.assertTrue(blocked)
        self.assertIn(SAMU_URL, self.send.await_args.args[1])
        self.assertIn(UPA_URL, self.send.await_args.args[1])

    async def test_direct_care_handler_rejects_memory_question_before_database(self):
        handled = await wa.handle_wa_care_message(sender_phone='synthetic', user_id='actor',
            user_name='Ana', msg_type='text', db=self.pool, user_text='O que voce lembra de mim?')
        self.assertFalse(handled)
        self.conn.execute.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_repeated_family_clarification_varies_but_keeps_candidates(self):
        result = OperationResult.clarification('Escolha Bruno ou 1.', data={'candidates': []})
        for _ in range(3):
            await wa._send_family_clarification('synthetic', result)
        outputs = [call.args[1] for call in self.send.await_args_list]
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertNotEqual(outputs[1], outputs[2])
        self.assertTrue(all('Bruno' in text for text in outputs))

    async def test_memory_query_reaches_preferences_without_proposal_or_send(self):
        with patch('services.wa_preferences.read_preferences', AsyncMock(return_value={'nickname': 'Ana'})), \
             patch('core.confirmation.create_pending_confirmation', AsyncMock()) as create, \
             patch.object(wa, 'handle_wa_care_message', AsyncMock()) as care:
            await self.incoming('O que voce lembra de mim?')
        self.assertTrue(self.send.await_count)
        self.assertIn('Não guardo apelidos nem preferências', self.send.await_args.args[1])
        create.assert_not_awaited()
        care.assert_not_awaited()

    async def test_actual_calendar_bypass_now_varies_response(self):
        with patch('services.calendar_reply.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
            await self.incoming('Que horas são?')
            await self.incoming('Que horas são?')
        outputs = [call.args[1] for call in self.send.await_args_list]
        self.assertEqual(len(outputs), 2)
        self.assertNotEqual(outputs[0], outputs[1])

    async def test_pressure_pipeline_does_not_create_buttons_or_pending(self):
        with patch.object(wa, 'send_confirmation_buttons', AsyncMock()) as buttons, \
             patch.object(wa, 'create_pending_confirmation', AsyncMock()) as create:
            await self.incoming('Minha pressão deu 70 por 35')
        self.assertIn('Diastólica: 35', self.send.await_args.args[1])
        buttons.assert_not_awaited()
        create.assert_not_awaited()

    async def test_confirmed_reading_uses_persisted_time_in_local_day(self):
        self.conn.fetchrow.side_effect = [
            {'metric_type': 'BATIMENTOS', 'status': 'pending', 'active': True},
            {'metric_type': 'BATIMENTOS', 'value_json': json.dumps({'batimentos': 99}),
             'confirmed_at': datetime(2026, 10, 14, 1, 4, tzinfo=timezone.utc)}]
        with patch.object(wa, 'resolve_linked_account', AsyncMock(return_value=self.identity)), \
             patch.object(wa, 'confirm_pending_confirmation', AsyncMock(return_value=True)):
            await wa.handle_confirmation_callback('confirm_SYNTHETIC', '5500000000000')
        text = self.send.await_args.args[1]
        self.assertIn('99 bpm', text)
        self.assertIn('13/10/2026 22:04', text)
        self.assertNotIn('{', text)

    async def test_callback_reports_progress_and_real_failure_or_success(self):
        for status, message in [('success', 'Mensagem enviada ao WhatsApp.'),
                                ('unavailable', 'Não consegui entregar a mensagem.')]:
            self.send.reset_mock()
            self.conn.fetchrow.side_effect = None
            self.conn.fetchrow.return_value = {'metric_type': 'CARE_MESSAGE', 'status': 'pending', 'active': True}
            with patch.object(wa, 'resolve_linked_account', AsyncMock(return_value=self.identity)), \
                 patch('services.care_messages.confirm_pending_care_message', AsyncMock(return_value=OperationResult(status=status, message=message))):
                await wa.handle_confirmation_callback('confirm_SYNTHETIC', '5500000000000')
            self.assertIn('em andamento', self.send.await_args_list[0].args[1])
            self.assertEqual(self.send.await_args.args[1], message)

    async def test_family_candidate_is_visible_without_selecting_wrong_person(self):
        self.conn.fetch.return_value = [{'patient_id': 'patient', 'full_name': 'Bruno Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        ctx = OperationContext(actor_id='actor', patient_id='actor')
        for text in ('Qual a pressão da mãe?', 'Me lembra de avisar a mãe', 'Tomei o remédio da mamãe'):
            target, result = await resolve_target_context(self.conn, ctx, text)
            self.assertIsNone(target)
            self.assertIn('Bruno', result.message)
            self.assertNotIn('Silva', result.message)
            self.assertNotIn('Não encontrei esse nome', result.message)
        wa._remember_operation('actor', 'Qual a pressão da mãe?', result, 'query_bp')
        target, error = await resolve_target_context(self.conn, ctx, wa._operation_followup('actor', '1'))
        self.assertIsNone(error)
        self.assertEqual(target.patient_id, 'patient')
