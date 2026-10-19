"""Aceite do contrato de lote em PostgreSQL real e efêmero."""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, HTTPException
import httpx

from api_identity import firebase_uid
from routers import care_os, sync
from schemas.medication_doses import DoseBatchConfirm, DoseBatchPrepare, DoseScheduleUpdate
from services import medication_doses as doses
from services.medication_take import take_medication

ROOT = Path(__file__).resolve().parents[2]
DSN = os.getenv('TEST_POSTGRES_URL')


@unittest.skipUnless(DSN, 'PostgreSQL efêmero disponível no CI')
class DosePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(DSN)
        if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path != '/braga_privacy_test':
            raise RuntimeError('Somente banco efêmero braga_privacy_test')
        self.control = await asyncpg.connect(DSN)
        await self.control.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        self.schema = 'doses_' + uuid4().hex
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')
        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}",public')
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / 'scripts/docker/init_schema.sql').read_text())
            for n in (3, 17, 21, 52, 55):
                p = next(p for p in (ROOT / 'migrations').glob(f'{n:03d}_*.sql') if not p.stem.endswith('_rollback'))
                await conn.execute(p.read_text(encoding='utf-8-sig'))
            await conn.execute("INSERT INTO profiles(id,full_name,timezone) VALUES ('patient','Maria','America/Sao_Paulo'),('actor','Ana','America/Manaus'),('other','Outro','UTC')")
            await conn.execute("INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,caregiver_role,expires_at) VALUES ('patient','actor','Ana','Filha','DOSES','ACTIVE','ADMIN_CHILD',now()+interval '1 day')")
        self.day = date(2026, 10, 13)

    async def asyncTearDown(self):
        await self.pool.close()
        await self.control.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.control.close()

    async def med(self, name='Losartana', units=1, times=('08:00',), stock=40, configured=True):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                med_id = await conn.fetchval('''INSERT INTO medications(user_id,name,schedule_time,schedule_times,
                     total_units,current_units,pharmaceutical_form) VALUES('patient',$1,$2,$3,$4,$4,'comprimido') RETURNING id''',
                     name, times[0], ','.join(times), stock)
                if configured:
                    await doses.update_schedule(conn, actor_id='actor', patient_id='patient', medication_id=med_id,
                        payload=DoseScheduleUpdate(expected_revision=1, slots=[{'local_time': t, 'dose_units': units, 'quantity_confirmed': True} for t in times]))
        return med_id

    async def prepare(self, *, local_time='08:00', actor='actor', client_id=None, selected=None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                data = await doses.materialize_groups(conn, actor_id=actor, patient_id='patient', day=self.day)
                group = next(g for g in data['groups'] if g['local_time'] == local_time)
                payload = DoseBatchPrepare(client_batch_id=client_id or uuid4(), group_id=group['group_id'],
                     expected_schedule_digest=group['schedule_digest'], occurrence_ids=selected or [i['occurrence_id'] for i in group['items']], channel='app')
                batch, _ = await doses.prepare_batch(conn, actor_id=actor, patient_id='patient', payload=payload)
        return batch, payload

    def confirmation(self, batch, selection=None):
        return DoseBatchConfirm(confirmation_id=uuid4(), snapshot_digest=batch['snapshot_digest'],
            selected_occurrence_ids=selection or [i['occurrence_id'] for i in batch['items']])

    async def confirm(self, batch, payload=None, actor='actor'):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await doses.confirm_batch(conn, actor_id=actor, batch_id=batch['batch_id'], payload=payload or self.confirmation(batch))

    async def stock(self, med_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT current_units FROM medications WHERE id=$1', med_id)

    async def test_twelve_meds_quantities_replay_and_result_recovery(self):
        med_ids = [await self.med(f'Medicamento {i}', units=(i % 3)+1) for i in range(12)]
        batch, prepare_payload = await self.prepare()
        self.assertEqual(len(batch['items']), 12)
        payload = self.confirmation(batch)
        result = await self.confirm(batch, payload)
        self.assertEqual(result['counts']['recorded'], 12)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result, await self.confirm(batch, payload))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                repeated, created = await doses.prepare_batch(conn, actor_id='actor', patient_id='patient', payload=prepare_payload)
                self.assertFalse(created)
                self.assertEqual(repeated, result)
                recovered = doses.batch_response(await doses.get_batch(conn, actor_id='actor', batch_id=batch['batch_id']))
                self.assertEqual(recovered, result)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 12)
        for i, mid in enumerate(med_ids):
            self.assertEqual(await self.stock(mid), 40-((i % 3)+1))
        changed = self.confirmation(batch, [batch['items'][0]['occurrence_id']])
        with self.assertRaises(HTTPException) as exc:
            await self.confirm(batch, changed)
        self.assertEqual(exc.exception.detail['code'], 'IDEMPOTENCY_CONFLICT')

    async def test_individual_batch_and_other_actor_concurrent_equivalent_offsets(self):
        mid = await self.med(stock=1)
        batch, _ = await self.prepare()
        other_batch, _ = await self.prepare(actor='patient')
        async def individual():
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    return await take_medication(conn, medication_id=str(mid).upper(), actor_id='patient',
                        scheduled_for=datetime.fromisoformat('2026-10-13T08:00:00-03:00'), units_taken=1, idempotency_key='individual-123')
        results = await asyncio.gather(self.confirm(batch), self.confirm(other_batch, actor='patient'), individual())
        self.assertEqual(await self.stock(mid), 0)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 1)
        self.assertEqual(sum(r.get('counts', {}).get('recorded', r.get('status') == 'recorded') for r in results), 1)

    async def test_independent_slots_and_units(self):
        mid = await self.med(times=('08:00','20:00'), units=2)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await doses.update_schedule(conn, actor_id='actor', patient_id='patient', medication_id=mid,
                    payload=DoseScheduleUpdate(expected_revision=2, slots=[{'local_time':'08:00','dose_units':2,'quantity_confirmed':True}, {'local_time':'20:00','dose_units':1,'quantity_confirmed':True}]))
        morning, _ = await self.prepare()
        evening, _ = await self.prepare(local_time='20:00')
        self.assertNotEqual(morning['items'][0]['occurrence_id'], evening['items'][0]['occurrence_id'])
        await self.confirm(morning); await self.confirm(evening)
        self.assertEqual(await self.stock(mid), 37)

    async def test_partial_stock_exclusion_and_new_batch_only_remaining(self):
        mids = [await self.med(f'Med {i}') for i in range(3)]
        batch, _ = await self.prepare()
        target = batch['items'][0]
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE medications SET current_units=0 WHERE id=$1::uuid', target['medication_id'])
        selection = [i['occurrence_id'] for i in batch['items'][:2]]
        result = await self.confirm(batch, self.confirmation(batch, selection))
        self.assertEqual(result['counts'], {'recorded':1,'already_recorded':0,'excluded':1,'blocked':1})
        self.assertEqual(result['state'], 'partially_confirmed')
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE medications SET current_units=10 WHERE id=$1::uuid', target['medication_id'])
        retry, _ = await self.prepare(selected=[target['occurrence_id']])
        self.assertEqual((await self.confirm(retry))['counts']['recorded'], 1)
        self.assertEqual(sum([await self.stock(m) for m in mids]), 88)

    async def test_three_individual_records_nine_new(self):
        for i in range(12):
            await self.med(f'Med {i}')
        batch, _ = await self.prepare()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for item in batch['items'][:3]:
                    await take_medication(conn, medication_id=item['medication_id'], actor_id='patient',
                        scheduled_for=datetime.fromisoformat(item['scheduled_at']), units_taken=1, idempotency_key='individual-'+item['medication_id'])
        result = await self.confirm(batch)
        self.assertEqual(result['counts']['recorded'], 9)
        self.assertEqual(result['counts']['already_recorded'], 3)

    async def test_unexpected_db_failure_rolls_back_all_items(self):
        await self.med('Med A'); await self.med('Med B')
        batch, _ = await self.prepare()
        original = take_medication
        calls = 0
        async def broken(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                await args[0].execute('SELECT 1/0')
            return await original(*args, **kwargs)
        with patch('services.medication_take.take_medication', side_effect=broken):
            with self.assertRaises(asyncpg.DivisionByZeroError):
                await self.confirm(batch)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 0)
            self.assertEqual(await conn.fetchval('SELECT sum(current_units) FROM medications'), 80)
            self.assertEqual(await conn.fetchval('SELECT state FROM medication_dose_batches WHERE id=$1::uuid', batch['batch_id']), 'prepared')

    async def test_changed_schedule_and_timezone_rejected_without_write(self):
        mid = await self.med()
        batch, _ = await self.prepare()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE medications SET meal_context='JEJUM' WHERE id=$1", mid)
        with self.assertRaises(HTTPException) as exc:
            await self.confirm(batch)
        self.assertEqual(exc.exception.detail['code'], 'SCHEDULE_CHANGED')
        self.assertEqual(await self.stock(mid), 40)
        fresh, _ = await self.prepare()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE profiles SET timezone='America/Manaus' WHERE id='patient'")
        with self.assertRaises(HTTPException):
            await self.confirm(fresh)
        self.assertEqual(await self.stock(mid), 40)

    async def test_revocation_expiration_wrong_actor_and_cancel(self):
        mid = await self.med()
        batch, _ = await self.prepare()
        with self.assertRaises(HTTPException) as exc:
            await self.confirm(batch, actor='other')
        self.assertEqual(exc.exception.status_code, 404)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='REVOKED'")
        with self.assertRaises(HTTPException) as exc:
            await self.confirm(batch)
        self.assertEqual(exc.exception.status_code, 403)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='ACTIVE'")
            await conn.execute("UPDATE medication_dose_batches SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", batch['batch_id'])
        with self.assertRaises(HTTPException) as exc:
            await self.confirm(batch)
        self.assertEqual(exc.exception.detail['code'], 'PROPOSAL_EXPIRED')
        fresh, _ = await self.prepare()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                result = await doses.cancel_batch(conn, actor_id='actor', batch_id=fresh['batch_id'])
                self.assertEqual(result['state'], 'cancelled')
        with self.assertRaises(HTTPException):
            await self.confirm(fresh)
        self.assertEqual(await self.stock(mid), 40)

    async def test_legacy_quantity_and_edit_slot_reset(self):
        mid = await self.med(configured=False)
        with self.assertRaises(HTTPException) as exc:
            await self.prepare()
        self.assertEqual(exc.exception.detail['code'], 'REVIEW_REQUIRED')
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await take_medication(conn, medication_id=str(mid), actor_id='patient',
                    scheduled_for=datetime.fromisoformat('2026-10-13T08:00:00-03:00'), units_taken=1, idempotency_key='legacy-12345')
                await doses.update_schedule(conn, actor_id='actor', patient_id='patient', medication_id=mid,
                    payload=DoseScheduleUpdate(expected_revision=1, slots=[{'local_time':'08:00','dose_units':1,'quantity_confirmed':True}]))
            await conn.execute("UPDATE medications SET schedule_time='20:00',schedule_times='20:00' WHERE id=$1", mid)
            await conn.execute("UPDATE medications SET schedule_time='08:00',schedule_times='08:00' WHERE id=$1", mid)
            slot = await conn.fetchrow("SELECT * FROM medication_dose_schedules WHERE medication_id=$1 AND local_time='08:00'", mid)
            self.assertFalse(slot['quantity_confirmed'])
            self.assertIsNone(slot['dose_units'])
            self.assertEqual(await conn.fetchval('SELECT schedule_revision FROM medications WHERE id=$1', mid), 4)

    async def test_snapshot_selection_revision_and_idempotency_conflicts(self):
        mid = await self.med()
        batch, payload = await self.prepare()
        wrong = self.confirmation(batch); wrong.snapshot_digest = '0'*64
        with self.assertRaises(HTTPException):
            await self.confirm(batch, wrong)
        wrong = self.confirmation(batch); wrong.selected_occurrence_ids = [uuid4()]
        with self.assertRaises(HTTPException):
            await self.confirm(batch, wrong)
        wrong = self.confirmation(batch); wrong.declared_taken_at = datetime.now(timezone.utc)+timedelta(days=1)
        with self.assertRaises(HTTPException):
            await self.confirm(batch, wrong)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                payload.expected_schedule_digest = "0"*64
                with self.assertRaises(HTTPException) as exc:
                    await doses.prepare_batch(conn, actor_id='actor', patient_id='patient', payload=payload)
                self.assertEqual(exc.exception.detail['code'], 'IDEMPOTENCY_CONFLICT')
            with self.assertRaises(HTTPException):
                async with conn.transaction():
                    await doses.update_schedule(conn, actor_id='actor', patient_id='patient', medication_id=mid,
                         payload=DoseScheduleUpdate(expected_revision=50, slots=[{'local_time':'08:00'}]))
        self.assertEqual(await self.stock(mid), 40)

    async def test_migration_preserves_offset_duplicates_and_flags_naive(self):
        mid = await self.med()
        async with self.pool.acquire() as conn:
            await conn.execute('ALTER TABLE medication_logs DISABLE TRIGGER medication_log_canonical_guard')
            await conn.execute('ALTER TABLE medication_logs DISABLE TRIGGER medication_log_link_occurrence')
            await conn.execute('''INSERT INTO medication_logs(user_id,medication_id,taken_at,scheduled_for)
                VALUES('patient',$1,now(),'2026-10-13T08:00:00-03:00'),
                ('patient',$1,now(),'2026-10-13T11:00:00+00:00'),
                ('patient',$1,now(),'2026-10-13T20:00')''', mid)
            await conn.execute('ALTER TABLE medication_logs ENABLE TRIGGER medication_log_canonical_guard')
            await conn.execute('ALTER TABLE medication_logs ENABLE TRIGGER medication_log_link_occurrence')
            await conn.execute((ROOT/'migrations/055_medication_dose_batches.sql').read_text())
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'), 3)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_dose_occurrences'), 1)
            self.assertTrue(await conn.fetchval('SELECT historical_conflict FROM medication_dose_occurrences'))
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_dose_migration_issues'), 1)
            oid = await conn.fetchval('SELECT id FROM medication_dose_occurrences')
            self.assertEqual(str(oid), doses.occurrence_id('patient', mid, datetime.fromisoformat('2026-10-13T11:00:00+00:00')))
            self.assertEqual(await conn.fetchval('SELECT current_units FROM medications WHERE id=$1', mid), 40)

    async def test_individual_different_quantity_never_overwrites(self):
        mid = await self.med(units=2)
        batch, _ = await self.prepare()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await take_medication(conn, medication_id=str(mid), actor_id='patient',
                    scheduled_for=datetime.fromisoformat(batch['items'][0]['scheduled_at']), units_taken=1,
                    idempotency_key='different-quantity')
        result = await self.confirm(batch)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['items'][0]['reason_code'], 'ALREADY_RECORDED_DIFFERENT_QUANTITY')
        self.assertEqual(await self.stock(mid), 39)

    async def test_android_legacy_sync_resolves_same_canonical_dose(self):
        mid = await self.med()
        batch, _ = await self.prepare()
        await self.confirm(batch)
        with patch.object(sync._state, 'db_pool', self.pool):
            result = await sync.sync_medication_log(sync.MedicationLogSyncDto(id=str(uuid4()),userId='patient',
                medicationId=str(mid), takenAt='2026-10-13T11:01:00Z',scheduledFor='2026-10-13T08:00'), uid='patient')
        self.assertEqual(result['dose_status'], 'already_recorded')
        self.assertEqual(await self.stock(mid), 39)

    async def test_http_contract_validation_and_full_flow(self):
        await self.med()
        app = FastAPI(); app.include_router(care_os.router)
        app.dependency_overrides[firebase_uid] = lambda: 'actor'
        with patch.object(care_os._state, 'db_pool', self.pool):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                data = (await client.get('/api/family/patients/patient/medication-dose-groups?date=2026-10-13')).json()
                group = data['groups'][0]
                body = {'client_batch_id':str(uuid4()),'group_id':group['group_id'],
                    'expected_schedule_digest':group['schedule_digest'],
                    'occurrence_ids':[i['occurrence_id'] for i in group['items']], 'channel':'app'}
                retired = await client.post('/api/family/patients/patient/medication-dose-batches',json={**body, 'channel':'whatsapp'})
                self.assertEqual(retired.status_code, 422)
                first = await client.post('/api/family/patients/patient/medication-dose-batches',json=body)
                self.assertEqual(first.status_code, 201, first.text)
                second = await client.post('/api/family/patients/patient/medication-dose-batches',json=body)
                self.assertEqual(second.status_code, 200, second.text)
                batch = first.json()
                confirmation = self.confirmation(batch).model_dump(mode='json')
                path = f"/api/medication-dose-batches/{batch['batch_id']}"
                confirmed = await client.post(path+'/confirm',json=confirmation)
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual((await client.get(path)).json(), confirmed.json())
                self.assertEqual((await client.post(path+'/cancel')).status_code, 409)
                malformed = await client.post(path+'/confirm',json={**confirmation, 'actor_id':'forged'})
                self.assertEqual(malformed.status_code, 422)
                self.assertEqual(malformed.json()['detail']['code'], 'INVALID_INPUT')
                with patch.dict(os.environ, {'MEDICATION_DOSE_BATCHES_ENABLED':'false'}):
                    self.assertFalse((await client.get('/api/medication-dose-capabilities')).json()['enabled'])
                    self.assertEqual((await client.get(path)).status_code, 200)
                    disabled = await client.post(path+'/confirm',json=confirmation)
                    self.assertEqual(disabled.status_code, 503)
                    self.assertEqual(disabled.json()['detail']['code'], 'CONTRACT_DISABLED')

    async def test_database_guards_and_instruction_review(self):
        mid = await self.med()
        batch, _ = await self.prepare()
        await self.confirm(batch)
        async with self.pool.acquire() as conn:
            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE medication_dose_batches SET state='prepared' WHERE id=$1::uuid", batch['batch_id'])
            with self.assertRaises(asyncpg.UniqueViolationError):
                await conn.execute("INSERT INTO medication_logs(user_id,medication_id,taken_at,occurrence_id,canonical_guard) VALUES('patient',$1,now(),$2::uuid,true)", mid, batch['items'][0]['occurrence_id'])
            await conn.execute("UPDATE medications SET meal_context='instrução livre pendente' WHERE id=$1", mid)
            data = await doses.materialize_groups(conn,actor_id='actor',patient_id='patient',day=self.day)
            self.assertEqual(data['groups'][0]['items'][0]['reason_code'], 'needs_review')
            await conn.execute("UPDATE medications SET schedule_time='inválido',schedule_times='inválido' WHERE id=$1",mid)
            data = await doses.materialize_groups(conn,actor_id='actor',patient_id='patient',day=self.day)
            self.assertEqual(data['review_items'][0]['reason_code'], 'schedule_invalid')

    async def test_database_guards_legacy_writer_during_deploy(self):
        mid = await self.med()
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO medication_logs(user_id,medication_id,taken_at,scheduled_for) VALUES('patient',$1,now(),'2026-10-13T08:00')", mid)
            with self.assertRaises(asyncpg.UniqueViolationError):
                await conn.execute("INSERT INTO medication_logs(user_id,medication_id,taken_at,scheduled_for) VALUES('patient',$1,now(),'2026-10-13T11:00:00Z')", mid)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM medication_logs'),1)
        batch, _ = await self.prepare()
        result = await self.confirm(batch)
        self.assertEqual(result['counts']['already_recorded'],1)
        self.assertEqual(await self.stock(mid),40)

    async def test_schedule_reader_preserves_unconfirmed_slots_and_permission(self):
        mid = await self.med(configured=False)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                data = await doses.read_schedule(conn, actor_id='actor', patient_id='patient', medication_id=mid)
                self.assertEqual(data['schedule_revision'],1)
                self.assertEqual(data['slots'],[{'local_time':'08:00','dose_units':None,'quantity_confirmed':False}])
                with self.assertRaises(HTTPException) as err:
                    await doses.read_schedule(conn,actor_id='other',patient_id='patient',medication_id=mid)
                self.assertEqual(err.exception.status_code,403)
