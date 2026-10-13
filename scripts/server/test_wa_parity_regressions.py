"""Regressões do ensaio WhatsApp: propostas, alvo e efeitos verificáveis."""
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from orb_router import detect_all
from services.operations import OperationContext, OperationResult
from services.family_target import _explicit_person_name, resolve_target_context
from services.wa_preferences import detect_preference_intent, confirm_pending_preference
from services.medication_lookup import detect_hour
from services.medication_take import resolve_scheduled_for, prepare_medication_take
from services.care_messages import prepare_care_message, confirm_pending_care_message
from routers.sync import BiometrySyncDto, sync_biometry


def connection():
    conn = MagicMock()
    for name in ('fetch', 'fetchrow', 'fetchval', 'execute'):
        setattr(conn, name, AsyncMock())
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    return conn


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class ParsingTests(unittest.TestCase):
    def test_water_query_is_not_a_250ml_record(self):
        self.assertEqual(detect_all('quanto de água bebi hoje?')['kind'], 'query_water')
        self.assertEqual(detect_all('bebi 200 ml de água')['kind'], 'hydration')

    def test_water_reminder_is_not_a_record(self):
        self.assertEqual(detect_all('me lembra de beber água')['kind'], 'reminder')

    def test_cup_preference_accepts_accents(self):
        self.assertEqual(detect_preference_intent('meu copo é de 200 ml'),
                         {'op': 'set', 'key': 'cup_ml', 'value': 200})

    def test_quantity_is_not_a_medication_time(self):
        self.assertIsNone(detect_hour('tomei duas capsulas de losartana'))
        self.assertEqual(detect_hour('tomei o remedio as 8h30'), '08:30')
        self.assertEqual(detect_hour('tomei o remedio das vinte e tres'), '23:00')
        self.assertIsNone(detect_hour('tomei o remedio das vinte e quatro'))

    def test_invalid_time_is_not_replaced_by_registered_schedule(self):
        self.assertIsNone(resolve_scheduled_for('tomei o remédio das 25h', '08:00'))
        self.assertIsNone(resolve_scheduled_for('tomei o remédio das vinte e quatro', '08:00'))
        self.assertEqual(detect_hour('tomei o remedio as 8 da noite'), '20:00')

    def test_take_by_name_without_generic_medication_word(self):
        self.assertEqual(detect_all('tomei losartana')['kind'], 'med_take')
        self.assertNotEqual(detect_all('tomei café')['kind'], 'med_take')

    def test_timezone_preserved(self):
        result = resolve_scheduled_for('tomei o remedio das 8', None, 'America/Manaus')
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.utcoffset().total_seconds(), -14400)


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_family_target(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': 'mother', 'full_name': 'Maria Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        for text in ('qual a pressão da Maria?', 'como está a mamãe Maria?'):
            ctx, error = await resolve_target_context(conn, OperationContext(actor_id='child', patient_id='child'), text)
            self.assertIsNone(error)
            self.assertEqual(ctx.patient_id, 'mother')
            self.assertEqual(ctx.actor_id, 'child')

    async def test_name_with_article_in_medication_query_resolves_family(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': 'yuri-id', 'full_name': 'Yuri Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        text = 'Quero saber se o Yuri tomou o remédio hoje de manhã'
        ctx, error = await resolve_target_context(
            conn, OperationContext(actor_id='caregiver', patient_id='caregiver'), text)
        self.assertIsNone(error)
        self.assertEqual(ctx.patient_id, 'yuri-id')
        self.assertEqual(ctx.actor_id, 'caregiver')
        self.assertIsNone(_explicit_person_name('qual a última pressão?'))
        self.assertIsNone(_explicit_person_name('tomei remédio hoje de manhã?'))

    async def test_unknown_named_relative_does_not_select_another_person(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': 'mother', 'full_name': 'Maria Silva', 'caregiver_role': 'ADMIN_CHILD'}]
        target, result = await resolve_target_context(conn, OperationContext(actor_id='child', patient_id='child'), 'qual a pressão do José?')
        self.assertIsNone(target)
        self.assertEqual(result.status, 'clarification')

    async def test_family_answer_continues_original_query(self):
        from routers.whatsapp import _remember_operation, _operation_followup, _WA_OPERATION_MEMORY
        self.addCleanup(_WA_OPERATION_MEMORY.clear)
        _remember_operation('actor', 'qual a pressão do familiar?', OperationResult.clarification('Qual?', data={'candidates': [{'full_name': 'Maria Silva'}]}), 'query_bp')
        text = _operation_followup('actor', 'Maria')
        self.assertEqual(detect_all(text)['kind'], 'query_bp')
        self.assertIn('da Maria', text)
        self.assertEqual(_operation_followup('other', 'Maria'), 'Maria')

    async def test_database_failure_does_not_mean_no_relatives(self):
        conn = connection()
        conn.fetch.side_effect = RuntimeError('database unavailable')
        _, result = await resolve_target_context(conn, OperationContext(actor_id='child', patient_id='child'), 'qual a pressão do papai?')
        self.assertEqual(result.status, 'unavailable')

    async def test_replaced_preference_does_not_claim_success(self):
        conn = connection()
        conn.fetchrow.side_effect = [{'id': 'pending', 'value_json': json.dumps({'key': 'nickname', 'value': 'Ana'})}, None]
        result = await confirm_pending_preference(Pool(conn), confirmation_code='OLD', actor_id='actor')
        self.assertEqual(result.status, 'missing_data')
        conn.execute.assert_not_awaited()

    async def test_preference_race_does_not_write_audit_success(self):
        conn = connection()
        conn.fetchrow.side_effect = [{'id': 'pending', 'value_json': json.dumps({'key': 'nickname', 'value': 'Ana'})}, {'proposal_id': 'CODE'}, None]
        conn.execute.return_value = 'UPDATE 1'
        result = await confirm_pending_preference(Pool(conn), confirmation_code='CODE', actor_id='actor')
        self.assertEqual(result.status, 'unavailable')
        self.assertFalse(any('INSERT INTO confirmation_logs' in call.args[0] for call in conn.execute.await_args_list))

    async def test_care_proposal_belongs_to_actor_and_names_patient(self):
        conn = connection()
        conn.fetchval.return_value = 'Maria Silva'
        context = OperationContext(actor_id='child', patient_id='mother')
        with patch('services.care_messages.validate_actor', AsyncMock(return_value=None)), \
             patch('core.confirmation.create_pending_confirmation', AsyncMock(return_value='CODE')) as create:
            result = await prepare_care_message(conn, Pool(conn), context=context, text='lembra a mamãe de beber água')
        self.assertEqual(result.status, 'confirmation')
        self.assertIn('Maria', result.message)
        self.assertEqual(create.await_args.kwargs['user_id'], 'child')
        self.assertEqual(create.await_args.kwargs['value_json']['patient_id'], 'mother')
        self.assertEqual(create.await_args.kwargs['value_json']['recipient_kind'], 'patient')

    async def test_care_delivery_uses_exact_patient(self):
        conn = connection()
        conn.fetchrow.return_value = {'id': 'pending', 'value_json': json.dumps({'body': 'Beba água', 'patient_id': 'mother', 'recipient_kind': 'patient'})}
        conn.fetchval.return_value = 'test-phone'
        conn.execute.return_value = 'UPDATE 1'
        with patch('services.care_messages.validate_actor', AsyncMock(return_value=None)), \
             patch('services.care_messages._claim', AsyncMock(return_value='claim')), \
             patch('services.care_messages._deliver_or_release', AsyncMock(return_value=True)) as deliver:
            result = await confirm_pending_care_message(Pool(conn), AsyncMock(), confirmation_code='CODE', actor_id='child')
        self.assertEqual(result.status, 'success')
        self.assertEqual(conn.fetchval.await_args.args[1], 'mother')
        self.assertEqual(deliver.await_args.args[3], 'test-phone')

    async def test_medication_quantity_and_actor_pending(self):
        conn = connection()
        conn.fetchval.return_value = 'America/Manaus'
        context = OperationContext(actor_id='child', patient_id='mother')
        with patch('services.medication_take.identify_medication', AsyncMock(return_value=OperationResult.success(data={'medication_id': 'med', 'name': 'Losartana', 'schedule_time': '08:00'}))), \
             patch('core.confirmation.create_pending_confirmation', AsyncMock(return_value='CODE')) as create:
            result = await prepare_medication_take(conn, Pool(conn), context=context, text='tomei duas cápsulas de losartana')
        self.assertEqual(result.data['units_taken'], 2)
        self.assertEqual(create.await_args.kwargs['user_id'], 'child')
        self.assertIn('08:00', result.data['scheduled_for'])
        self.assertTrue(result.data['scheduled_for'].endswith('-04:00'))

    async def test_preference_proposal_is_shown_before_buttons(self):
        from routers import whatsapp
        conn = connection()
        proposal = OperationResult.confirmation('Posso guardar o apelido Ana?', 'CODE', data={'key': 'nickname', 'value': 'Ana'})
        with patch('services.wa_preferences.handle_preference', AsyncMock(return_value=proposal)), \
             patch.object(whatsapp, 'send_whatsapp_text', AsyncMock()) as send, \
             patch.object(whatsapp, 'send_confirmation_buttons', AsyncMock()) as buttons, \
             patch.object(whatsapp, 'log_fine_tuning_sample'):
            handled = await whatsapp.handle_wa_preference(sender_phone='test', user_id='actor', user_name='Ana', msg_type='text', db=Pool(conn), user_text='me chama de Ana')
        self.assertTrue(handled)
        send.assert_awaited_once_with('test', proposal.message)
        self.assertEqual(buttons.await_args.kwargs['confirmation_code'], 'CODE')

    async def test_biometry_rejects_wrong_owner_before_database(self):
        value = BiometrySyncDto(id='00000000-0000-0000-0000-000000000001', userId='other', weight=75, measuredAt='2026-10-13T12:00:00Z')
        with self.assertRaises(HTTPException) as raised:
            await sync_biometry(value, uid='actor')
        self.assertEqual(raised.exception.status_code, 403)
