"""Confirmações e sincronização reais no banco efêmero do CI."""
import json
import os
from pathlib import Path
import unittest
import uuid
from urllib.parse import urlparse
from unittest.mock import AsyncMock, patch

import asyncpg
from core.confirmation import confirm_pending_confirmation
from routers import sync
from services.care_messages import prepare_care_message, confirm_pending_care_message
from services.health_queries import last_weight, last_blood_pressure, day_summary
from services.medication_take import prepare_medication_take, confirm_pending_medication_take
from services.operations import OperationContext
from services.wa_preferences import handle_preference, confirm_pending_preference

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
        await self.control.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')

        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}", public')
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / 'scripts/docker/init_schema.sql').read_text())
            for number in (3, 4, 9, 17, 18, 52, 53):
                migration = next(p for p in (ROOT / 'migrations').glob(f'{number:03d}_*.sql') if not p.stem.endswith('_rollback'))
                await conn.execute(migration.read_text(encoding='utf-8-sig'))
            await conn.execute("INSERT INTO profiles(id,full_name,height,whatsapp_phone,timezone) VALUES ('actor','Ana',170,'test-actor','America/Manaus'),('patient','Maria',165,'test-patient','America/Sao_Paulo')")
            await conn.execute("INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,caregiver_role,expires_at) VALUES ('patient','actor','Ana','Filha','TEST','ACTIVE','ADMIN_CHILD',now()+interval '1 day')")
        self.context = OperationContext(actor_id='actor', patient_id='actor')

    async def asyncTearDown(self):
        await self.pool.close()
        await self.control.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
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
