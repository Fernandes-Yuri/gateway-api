"""Confirmações e sincronização reais no banco efêmero do CI."""
import json
import asyncio
import os
from pathlib import Path
import unittest
import uuid
import tempfile
from urllib.parse import urlparse
from unittest.mock import AsyncMock, patch

import asyncpg
import httpx
from fastapi import FastAPI
from core.confirmation import confirm_pending_confirmation
from routers import sync
from services.care_messages import prepare_care_message, confirm_pending_care_message
from services.health_queries import last_weight, last_blood_pressure, day_summary
from services.medication_take import prepare_medication_take, confirm_pending_medication_take
from services.operations import OperationContext
from services.wa_preferences import handle_preference, confirm_pending_preference
from routers import whatsapp as wa
from test_whatsapp_bateria import callback, envelope

ROOT = Path(__file__).resolve().parents[2]
DSN = os.getenv('TEST_POSTGRES_URL')


@unittest.skipUnless(DSN, 'PostgreSQL efêmero disponível no CI')
class WhatsAppPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(DSN)
        if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
            raise RuntimeError('Somente banco efêmero braga_privacy_test')
        self.schema = 'wa_' + uuid.uuid4().hex
        self.control = await asyncpg.connect(DSN)
        self.pool = None
        self.addAsyncCleanup(self.cleanup_database)
        await self.control.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')

        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}", public')
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / 'scripts/docker/init_schema.sql').read_text())
            for number in (3, 4, 9, 17, 18, 21, 52, 53, 54, 55):
                migration = next(p for p in (ROOT / 'migrations').glob(f'{number:03d}_*.sql') if not p.stem.endswith('_rollback'))
                await conn.execute(migration.read_text(encoding='utf-8-sig'))
            await conn.execute("INSERT INTO profiles(id,full_name,height,whatsapp_phone,timezone) VALUES ('actor','Ana',170,'test-actor','America/Manaus'),('patient','Maria',165,'test-patient','America/Sao_Paulo')")
            await conn.execute("INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,caregiver_role,expires_at) VALUES ('patient','actor','Ana','Filha','TEST','ACTIVE','ADMIN_CHILD',now()+interval '1 day')")
        self.context = OperationContext(actor_id='actor', patient_id='actor')

    async def cleanup_database(self):
        if self.pool:
            await self.pool.close()
        await self.control.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        await self.control.close()

    async def test_preference_confirm_and_stale_button(self):
        async with self.pool.acquire() as conn:
            first = await handle_preference(conn, self.pool, context=self.context, text='me chama de Ana')
            second = await handle_preference(conn, self.pool, context=self.context, text='meu copo é de 200 ml')
        self.assertEqual(first.status, 'confirmation')
        self.assertEqual(second.status, 'confirmation')
        stale = await confirm_pending_preference(self.pool, confirmation_code=first.pending_op_id, actor_id='actor')
        self.assertEqual(stale.status, 'missing_data')
        confirmed = await confirm_pending_preference(self.pool, confirmation_code=second.pending_op_id, actor_id='actor')
        self.assertEqual(confirmed.status, 'success')
        repeated = await confirm_pending_preference(self.pool, confirmation_code=second.pending_op_id, actor_id='actor')
        self.assertEqual(repeated.status, 'missing_data')
        async with self.pool.acquire() as conn:
            self.assertEqual(json.loads(await conn.fetchval("SELECT preferences FROM assistant_preferences WHERE user_id='actor'")), {'cup_ml': 200})
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM confirmation_logs'), 1)

    async def test_medication_confirmation_decrements_stock_once(self):
        async with self.pool.acquire() as conn:
            med_id = await conn.fetchval("INSERT INTO medications(user_id,name,schedule_time,current_units,total_units) VALUES ('actor','Losartana','08:00',10,10) RETURNING id")
            proposal = await prepare_medication_take(conn, self.pool, context=self.context, text='tomei duas cápsulas de losartana das 8')
        self.assertEqual(proposal.status, 'confirmation')
        result = await confirm_pending_medication_take(self.pool, confirmation_code=proposal.pending_op_id, actor_id='actor')
        self.assertEqual(result.status, 'success', result.message)
        repeated = await confirm_pending_medication_take(self.pool, confirmation_code=proposal.pending_op_id, actor_id='actor')
        self.assertEqual(repeated.status, 'missing_data')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT current_units FROM medications WHERE id=$1', med_id), 8)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 1)

    async def test_family_care_uses_patient_and_revocation_stops_send(self):
        context = OperationContext(actor_id='actor', patient_id='patient')
        async with self.pool.acquire() as conn:
            proposal = await prepare_care_message(conn, self.pool, context=context, text='Lembre de beber água')
        sender = AsyncMock(return_value=True)
        result = await confirm_pending_care_message(self.pool, sender, confirmation_code=proposal.pending_op_id, actor_id='actor')
        self.assertEqual(result.status, 'success', result.message)
        sender.assert_awaited_once_with('test-patient', 'Lembre de beber água', 'CARE_MESSAGE')
        async with self.pool.acquire() as conn:
            another = await prepare_care_message(conn, self.pool, context=context, text='Beba água novamente')
            await conn.execute("UPDATE family_bindings SET status='REVOKED'")
            denied = await last_blood_pressure(conn, context)
        self.assertEqual(denied.status, 'denied')
        stopped = await confirm_pending_care_message(self.pool, sender, confirmation_code=another.pending_op_id, actor_id='actor')
        self.assertEqual(stopped.status, 'denied')
        self.assertEqual(sender.await_count, 1)

    async def test_weight_shared_destination_and_idempotent_app_upload(self):
        from core.confirmation import create_pending_confirmation
        code = await create_pending_confirmation('actor', 'PESO', {'peso_kg': 75.5}, self.pool)
        self.assertIsNotNone(code)
        self.assertTrue(await confirm_pending_confirmation(code, 'actor', self.pool))
        self.assertFalse(await confirm_pending_confirmation(code, 'actor', self.pool))
        async with self.pool.acquire() as conn:
            weight = await last_weight(conn, self.context)
            self.assertEqual(weight.status, 'success', weight.message)
            self.assertEqual(weight.data['value'], '75,5')
            summary = await day_summary(conn, self.context)
            self.assertEqual(summary.status, 'success', summary.message)
            self.assertEqual(len(summary.data['readings']), 1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM biometrics'), 1)
        value = sync.BiometrySyncDto(id=uuid.uuid4(), userId='actor', weight=76, height=170, measuredAt='2026-10-13T12:00:00Z')
        with patch.object(sync._state, 'db_pool', self.pool):
            await sync.sync_biometry(value, uid='actor')
            await sync.sync_biometry(value, uid='actor')
            readings = await sync.get_biometry('actor', uid='actor')
        self.assertEqual(len(readings), 2)
        uploaded = next(r for r in readings if r['id'] == str(value.id))
        self.assertEqual(uploaded['height'], 1.7)
        self.assertEqual(uploaded['weight'], 76)

    async def deliver_button(self, code, action='confirm', phone='5500000000000'):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE profiles SET whatsapp_phone='5500000000000' WHERE id='actor'")
        app = FastAPI()
        app.include_router(wa.router)
        send = AsyncMock()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(wa, 'WEBHOOK_OUTBOX_PATH', str(Path(temp) / 'events.jsonl')), \
             patch.object(wa, 'verify_meta_signature', return_value=True), \
             patch.object(wa, 'mark_whatsapp_message_read', AsyncMock()), \
             patch.object(wa._state, 'db_pool', self.pool), \
             patch.object(wa, 'send_whatsapp_text', send):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                result = await client.post('/api/whatsapp/webhook', json=envelope([callback(action, code, phone)]))
        wa.WHATSAPP_EVENT_LOG.clear()
        self.assertEqual(result.status_code, 200)
        self.assertGreater(send.await_count, 0, 'callback deve ter retorno visível')
        return send.await_args.args[1]

    async def test_real_webhook_confirms_vitals_once_and_api_exposes_them(self):
        from core.confirmation import create_pending_confirmation
        cases = [('PRESSAO', {'sistolica': 120, 'diastolica': 80}),
                 ('GLICEMIA', {'glicemia': 95}), ('AGUA', {'quantidade_ml': 500}),
                 ('PESO', {'peso_kg': 78.5}), ('BATIMENTOS', {'batimentos': 72}),
                 ('OXIGENACAO', {'saturacao': 98})]
        for metric, values in cases:
            with self.subTest(metric=metric):
                code = await create_pending_confirmation('actor', metric, values, self.pool)
                self.assertIn('Registro confirmado', await self.deliver_button(code))
                self.assertIn('já foi confirmada', await self.deliver_button(code))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM vital_signs WHERE user_id='actor'"), 5)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM biometrics WHERE user_id='actor'"), 1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM confirmation_logs'), 6)
        with patch.object(sync._state, 'db_pool', self.pool):
            vitals = await sync.get_vitals('actor', uid='actor', limit=50, offset=0, before=None)
            weight = await sync.get_biometry('actor', uid='actor')
        self.assertEqual(len(vitals), 5)
        self.assertEqual(weight[0]['weight'], 78.5)

    async def test_cancel_edit_expiration_and_foreign_actor_never_write(self):
        from core.confirmation import create_pending_confirmation
        for action in ('cancel', 'edit'):
            for metric in ('PRESSAO', 'GLICEMIA', 'AGUA', 'PESO', 'BATIMENTOS', 'OXIGENACAO', 'MED_TOMADA', 'PREFERENCE', 'CARE_MESSAGE'):
                code = await create_pending_confirmation('actor', metric, {}, self.pool)
                reply = await self.deliver_button(code, action)
                self.assertTrue('cancelado' in reply or 'Edição' in reply or 'dados corretos' in reply)
                self.assertIn('já foi cancelada', await self.deliver_button(code))
        code = await create_pending_confirmation('actor', 'AGUA', {'quantidade_ml': 500}, self.pool)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE pending_confirmations SET expires_at=now()-interval '1 second' WHERE confirmation_code=$1", code)
        self.assertIn('expirou', await self.deliver_button(code))
        foreign = await create_pending_confirmation('patient', 'AGUA', {'quantidade_ml': 500}, self.pool)
        self.assertIn('sua conta', await self.deliver_button(foreign))
        self.assertIn('Vincule', await self.deliver_button(foreign, phone='5500000000001'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM vital_signs'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM biometrics'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM confirmation_logs'), 0)

    async def test_real_webhook_medication_stock_and_sync_once(self):
        async with self.pool.acquire() as conn:
            med_id = await conn.fetchval("INSERT INTO medications(user_id,name,schedule_time,current_units,total_units) VALUES ('actor','Losartana','08:00',10,10) RETURNING id")
            proposal = await prepare_medication_take(conn, self.pool, context=self.context, text='tomei duas cápsulas de losartana das 8')
        self.assertEqual(proposal.status, 'confirmation')
        await self.deliver_button(proposal.pending_op_id)
        self.assertIn('já foi confirmada', await self.deliver_button(proposal.pending_op_id))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT current_units FROM medications WHERE id=$1', med_id), 8)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 1)
        with patch.object(sync._state, 'db_pool', self.pool):
            logs = await sync.get_medication_logs('actor', uid='actor')
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['units_taken'], 2)

    async def test_forget_all_with_pending_proposal_requires_confirmation(self):
        async with self.pool.acquire() as conn:
            first = await handle_preference(conn, self.pool, context=self.context, text='me chama de Ana')
        await self.deliver_button(first.pending_op_id)
        async with self.pool.acquire() as conn:
            await handle_preference(conn, self.pool, context=self.context, text='meu copo é de 200 ml')
            forget = await handle_preference(conn, self.pool, context=self.context, text='Esquece tudo')
            self.assertEqual(forget.status, 'confirmation')
            self.assertEqual(json.loads(await conn.fetchval("SELECT preferences FROM assistant_preferences WHERE user_id='actor'")), {'nickname': 'ana'})
        await self.deliver_button(forget.pending_op_id, 'cancel')
        async with self.pool.acquire() as conn:
            self.assertEqual(json.loads(await conn.fetchval("SELECT preferences FROM assistant_preferences WHERE user_id='actor'")), {'nickname': 'ana'})
            forget = await handle_preference(conn, self.pool, context=self.context, text='Esquece tudo')
        await self.deliver_button(forget.pending_op_id)
        async with self.pool.acquire() as conn:
            self.assertEqual(json.loads(await conn.fetchval("SELECT preferences FROM assistant_preferences WHERE user_id='actor'")), {})

    async def test_edit_preference_and_care_always_have_feedback_without_delivery(self):
        async with self.pool.acquire() as conn:
            pref = await handle_preference(conn, self.pool, context=self.context, text='me chama de Ana')
            care = await prepare_care_message(conn, self.pool, context=self.context, text='Lembre de beber água')
        sender = AsyncMock(return_value=True)
        with patch.object(wa._state, 'send_care_message', sender):
            for proposal in (pref, care):
                self.assertIn('dados corretos', await self.deliver_button(proposal.pending_op_id, 'edit'))
                self.assertIn('já foi cancelada', await self.deliver_button(proposal.pending_op_id))
        sender.assert_not_awaited()

    async def test_real_webhook_care_delivery_is_idempotent(self):
        async with self.pool.acquire() as conn:
            proposal = await prepare_care_message(conn, self.pool,
                context=OperationContext(actor_id='actor', patient_id='patient'), text='Lembre de beber água')
        sender = AsyncMock(return_value=True)
        with patch.object(wa._state, 'send_care_message', sender):
            self.assertIn('enviada ao WhatsApp', await self.deliver_button(proposal.pending_op_id))
            self.assertIn('já foi confirmada', await self.deliver_button(proposal.pending_op_id))
        sender.assert_awaited_once_with('test-patient', 'Lembre de beber água', 'CARE_MESSAGE')

    async def test_database_write_failure_rolls_back_and_can_retry(self):
        from core.confirmation import create_pending_confirmation
        code = await create_pending_confirmation('actor', 'AGUA', {'quantidade_ml': 500}, self.pool)
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE FUNCTION fail_vital_insert() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'synthetic write failure'; END; $$ LANGUAGE plpgsql;
                CREATE TRIGGER fail_insert BEFORE INSERT ON vital_signs
                FOR EACH ROW EXECUTE FUNCTION fail_vital_insert();""")
        self.assertIn('continua pendente', await self.deliver_button(code))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM vital_signs'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM confirmation_logs'), 0)
            self.assertEqual(await conn.fetchval('SELECT status FROM pending_confirmations WHERE confirmation_code=$1', code), 'pending')
            await conn.execute('DROP TRIGGER fail_insert ON vital_signs')
        self.assertIn('Registro confirmado', await self.deliver_button(code))

    async def test_family_queries_do_not_read_health_data_for_unknown_kinship(self):
        from services.family_target import resolve_target_context
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO vital_signs(user_id,systolic_pressure,diastolic_pressure,measured_at) VALUES ('patient',120,80,now())")
            target, result = await resolve_target_context(conn, self.context, 'qual a pressão da mamãe?')
            self.assertIsNone(target)
            self.assertEqual(result.status, 'clarification')
            target, error = await resolve_target_context(conn, self.context, 'qual a pressão da Maria?')
            self.assertIsNone(error)
            self.assertEqual((await last_blood_pressure(conn, target)).status, 'success')
            await conn.execute("UPDATE family_bindings SET status='REVOKED'")
            self.assertEqual((await last_blood_pressure(conn, target)).status, 'denied')

    async def test_revocation_before_callback_prevents_care_delivery(self):
        async with self.pool.acquire() as conn:
            proposal = await prepare_care_message(conn, self.pool,
                context=OperationContext(actor_id='actor', patient_id='patient'), text='Lembre de beber água')
            await conn.execute("UPDATE family_bindings SET status='REVOKED'")
        sender = AsyncMock(return_value=True)
        with patch.object(wa._state, 'send_care_message', sender):
            reply = await self.deliver_button(proposal.pending_op_id)
        self.assertNotIn('entregue', reply)
        sender.assert_not_awaited()

    async def test_care_delivery_failure_is_not_reported_as_delivered_or_retried(self):
        async with self.pool.acquire() as conn:
            proposal = await prepare_care_message(conn, self.pool,
                context=OperationContext(actor_id='actor', patient_id='patient'), text='Lembre de beber água')
        sender = AsyncMock(return_value=False)
        with patch.object(wa._state, 'send_care_message', sender):
            self.assertIn('Não consegui entregar', await self.deliver_button(proposal.pending_op_id))
            self.assertIn('já foi confirmada', await self.deliver_button(proposal.pending_op_id))
        self.assertEqual(sender.await_count, 1)

    async def test_concurrent_callbacks_persist_once(self):
        from core.confirmation import create_pending_confirmation
        code = await create_pending_confirmation('actor', 'AGUA', {'quantidade_ml': 500}, self.pool)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE profiles SET whatsapp_phone='5500000000000' WHERE id='actor'")
        with patch.object(wa._state, 'db_pool', self.pool), \
             patch.object(wa, 'send_whatsapp_text', AsyncMock()) as send:
            await asyncio.gather(*(wa.handle_confirmation_callback(f'confirm_{code}', '5500000000000') for _ in range(2)))
        self.assertEqual(send.await_count, 2)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM vital_signs'), 1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM confirmation_logs'), 1)
