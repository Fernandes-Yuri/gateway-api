"""Regressões dos eventos e intenções observados na bateria de 13/10."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from routers import whatsapp as wa
from services.family_target import resolve_target_context
from services.operations import OperationContext
from test_wa_parity_regressions import connection
from services.calendar_reply import calendar_reply
from orb_router import detect_all, whatsapp_next
from services.wa_preferences import handle_preference


def callback(action='confirm', code='TEST', phone='5500000000000', kind='button_reply'):
    return {'id': 'synthetic-event', 'from': phone, 'type': 'interactive',
            'interactive': {'type': kind, kind: {'id': f'{action}_{code}', 'title': action}}}


def envelope(messages):
    return {'entry': [{'changes': [{'value': {'messages': messages}}]}]}


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.include_router(wa.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for item in (patch.object(wa, 'WEBHOOK_OUTBOX_PATH', str(Path(temp.name) / 'events.jsonl')),
                     patch.object(wa, 'verify_meta_signature', return_value=True)):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(wa.WHATSAPP_EVENT_LOG.clear)

    async def test_real_button_reply_dispatches_all_actions_and_messages(self):
        messages = [callback(action, code=str(i)) for i, action in enumerate(('confirm', 'cancel', 'edit'))]
        messages.append({'type': 'text', 'from': '5500000000000', 'text': {'body': 'Olá'}})
        with patch.object(wa, 'handle_confirmation_callback', AsyncMock()) as handler, \
             patch.object(wa, 'process_incoming_whatsapp_payload', AsyncMock()) as incoming:
            result = await self.client.post('/api/whatsapp/webhook', json=envelope(messages))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(handler.await_count, 3)
        self.assertEqual([call.kwargs['button_id'] for call in handler.await_args_list],
                         ['confirm_0', 'cancel_1', 'edit_2'])
        incoming.assert_awaited_once_with(envelope([messages[-1]]))

    async def test_batch_keeps_text_and_callback_order(self):
        order = []
        text = {'type': 'text', 'from': '5500000000000', 'text': {'body': 'nova proposta'}}

        async def incoming(payload):
            order.append('text')

        async def handler(**kwargs):
            order.append(kwargs['button_id'])

        with patch.object(wa, 'handle_confirmation_callback', side_effect=handler), \
             patch.object(wa, 'process_incoming_whatsapp_payload', side_effect=incoming):
            await self.client.post('/api/whatsapp/webhook', json=envelope([text, callback()]))
        self.assertEqual(order, ['text', 'confirm_TEST'])

    async def test_legacy_button_remains_supported(self):
        with patch.object(wa, 'handle_confirmation_callback', AsyncMock()) as handler, \
             patch.object(wa, 'process_incoming_whatsapp_payload', AsyncMock()):
            await self.client.post('/api/whatsapp/webhook', json=envelope([callback(kind='button')]))
        handler.assert_awaited_once_with(button_id='confirm_TEST', sender_phone='5500000000000')

    async def test_invalid_signature_never_dispatches(self):
        with patch.object(wa, 'verify_meta_signature', return_value=False), \
             patch.object(wa, 'handle_confirmation_callback', AsyncMock()) as handler:
            result = await self.client.post('/api/whatsapp/webhook', json=envelope([callback()]))
        self.assertEqual(result.status_code, 401)
        handler.assert_not_awaited()

    async def test_invalid_callback_and_database_unavailable_have_feedback(self):
        with patch.object(wa._state, 'db_pool', None), \
             patch.object(wa, 'send_whatsapp_text', AsyncMock()) as send:
            for button_id in ('unknown_TEST', 'confirm_', 'malformed', 'confirm_TEST'):
                await wa.handle_confirmation_callback(button_id, '5500000000000')
        self.assertEqual(send.await_count, 4)


class FamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_unique_binding_never_selects_patient(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': 'spouse', 'full_name': 'Bruno Silva',
                                   'caregiver_role': 'ADMIN_CHILD'}]
        context = OperationContext(actor_id='actor', patient_id='actor')
        for subject in ('mamãe', 'meu pai', 'minha filha', 'ela', 'minha avó', 'mãe', 'pai', 'familiar mamãe'):
            with self.subTest(subject=subject):
                target, result = await resolve_target_context(conn, context, f'qual a pressão de {subject}?')
                self.assertIsNone(target)
                self.assertEqual(result.status, 'clarification')
        conn.fetchval.assert_not_awaited()
        conn.execute.assert_not_awaited()

    async def test_choice_after_kinship_clarification_resolves_named_patient(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': 'spouse', 'full_name': 'Bruno Silva',
                                   'caregiver_role': 'ADMIN_CHILD'}]
        context = OperationContext(actor_id='actor', patient_id='actor')
        _, result = await resolve_target_context(conn, context, 'qual a pressão da mamãe?')
        with patch.dict(wa._WA_OPERATION_MEMORY, clear=True):
            wa._remember_operation('actor', 'qual a pressão da mamãe?', result, 'query_bp')
            text = wa._operation_followup('actor', 'Bruno')
        target, error = await resolve_target_context(conn, context, text)
        self.assertIsNone(error)
        self.assertEqual(target.patient_id, 'spouse')

    async def test_ambiguous_names_do_not_select_first(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': str(i), 'full_name': name, 'caregiver_role': 'ADMIN_CHILD'}
                                  for i, name in enumerate(('Ana Silva', 'Ana Souza'))]
        target, result = await resolve_target_context(
            conn, OperationContext(actor_id='actor', patient_id='actor'), 'qual a pressão da Ana?')
        self.assertIsNone(target)
        self.assertEqual(result.status, 'clarification')

    async def test_same_first_name_choices_are_reduced_and_selectable(self):
        conn = connection()
        conn.fetch.return_value = [{'patient_id': str(i), 'full_name': name, 'caregiver_role': 'ADMIN_CHILD'}
                                  for i, name in enumerate(('Ana Silva', 'Ana Souza'))]
        context = OperationContext(actor_id='actor', patient_id='actor')
        _, result = await resolve_target_context(conn, context, 'qual a pressão da Ana?')
        self.assertNotIn('Silva', result.message)
        self.assertNotIn('Souza', result.message)
        self.assertIn('opção 2', result.message)
        with patch.dict(wa._WA_OPERATION_MEMORY, clear=True):
            wa._remember_operation('actor', 'qual a pressão da Ana?', result, 'query_bp')
            text = wa._operation_followup('actor', 'opção 2')
        target, error = await resolve_target_context(conn, context, text)
        self.assertIsNone(error)
        self.assertEqual(target.patient_id, '1')


class IntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_forget_all_supersedes_pending_instead_of_cancelling(self):
        conn = connection()
        conn.fetchrow.return_value = {'proposal_id': 'OLD'}
        with patch('services.wa_preferences.read_preferences', AsyncMock(return_value={'nickname': 'Ana'})), \
             patch('core.confirmation.create_pending_confirmation', AsyncMock(return_value='NEW')) as create, \
             patch('services.wa_preferences._cancel_pending', AsyncMock()) as cancel:
            result = await handle_preference(conn, None, context=OperationContext(actor_id='actor', patient_id='actor'), text='Esquece tudo')
        self.assertEqual(result.status, 'confirmation')
        self.assertIn('todas as preferências', result.message)
        self.assertEqual(create.await_args.kwargs['value_json']['key'], 'all')
        cancel.assert_not_awaited()

    async def test_future_reminder_does_not_create_immediate_delivery_proposal(self):
        from services.care_messages import prepare_care_message
        with patch('services.care_messages.validate_actor', AsyncMock(return_value=None)), \
             patch('core.confirmation.create_pending_confirmation', AsyncMock()) as create:
            result = await prepare_care_message(connection(), None,
                context=OperationContext(actor_id='actor', patient_id='actor'), text='Me lembra amanhã de tomar o remédio')
        self.assertEqual(result.status, 'clarification')
        self.assertIn('horário futuro', result.message)
        create.assert_not_awaited()

    def test_medication_reminder_and_future_intent_are_distinct_from_prescription(self):
        self.assertEqual(detect_all('Lembre a mamãe de tomar o remédio')['kind'], 'reminder')
        self.assertEqual(detect_all('Vou tomar o remédio às 8')['kind'], 'med_plan')
        reply = whatsapp_next('Vou tomar o remédio às 8', True)
        self.assertEqual(reply['type'], 'text')
        self.assertNotIn('não receito', reply['fala'])
        self.assertEqual(detect_all('Tomei o remédio às 8')['kind'], 'med_take')

    def test_calendar_uses_correct_weekday_and_account_timezone(self):
        now = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)
        reply = calendar_reply('Que dia é hoje, 20/09/2026?', 'date', 'America/Sao_Paulo', now)
        self.assertIn('terça-feira, 13 de outubro de 2026', reply)
        self.assertEqual(calendar_reply('Que horas são?', 'time', 'America/Manaus', now), 'Agora são 22 horas e 30 minutos.')
        self.assertIn('domingo', calendar_reply('Que dia é 20/09/2026?', 'date', now=now))
        self.assertIn('não é válida', calendar_reply('Que dia é 31/02/2026?', 'date', now=now))
        self.assertEqual(whatsapp_next('Que dia é hoje, 20/09/2026?', True)['type'], 'text')
