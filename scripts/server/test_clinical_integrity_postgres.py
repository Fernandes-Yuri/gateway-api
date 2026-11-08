"""Contratos clínicos no PostgreSQL efêmero do Actions, sem contas reais."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import HTTPException
from routers import consultations, sync


@unittest.skipUnless(os.getenv('TEST_POSTGRES_URL'), 'PostgreSQL exclusivo do CI')
class ClinicalIntegrityPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.environ['TEST_POSTGRES_URL']
        parsed = urlparse(dsn)
        if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path != '/braga_privacy_test':
            raise RuntimeError('Este teste aceita somente o banco efêmero do CI')
        self.schema = 'clinical_' + uuid.uuid4().hex
        self.control = await asyncpg.connect(dsn)
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')

        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}", public')

        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, setup=setup)
        root = Path(__file__).resolve().parents[2]
        async with self.pool.acquire() as conn:
            await conn.execute((root / 'scripts/docker/init_schema.sql').read_text())
            await conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)')
            for file in ('054_vital_signs_oxygen.sql', '080_clinical_sync_integrity.sql', '081_vital_signs_readings_contract.sql'):
                await conn.execute((root / 'migrations' / file).read_text())
            await conn.execute("INSERT INTO profiles(id,full_name) VALUES('patient','Paciente'),('caregiver','Cuidador'),('stranger','Outro')")
            await conn.execute("INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,expires_at) VALUES('patient','caregiver','Ana','Filha','TEST1234','ACTIVE',now()+interval '1 day')")
        self.state = SimpleNamespace(db_pool=self.pool, rate_limiter=None, dispatch_clinical_alert=AsyncMock())
        self.sync_patch = patch.object(sync, '_state', self.state)
        self.sync_patch.start()
        self.ip_patch = patch.object(sync, 'get_client_ip', return_value='127.0.0.1')
        self.ip_patch.start()
        consultations.configure_state(db_pool=self.pool)

    async def asyncTearDown(self):
        self.ip_patch.stop()
        self.sync_patch.stop()
        consultations.configure_state()
        await self.pool.close()
        await self.control.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.control.close()

    def vital(self, **changes):
        payload = dict(id=uuid.uuid4(), userId='patient', hydrationMl=250,
                       measuredAt=datetime.now(timezone.utc).isoformat())
        payload.update(changes)
        return sync.VitalSignSyncDto(**payload)

    async def upload(self, vital):
        return await sync.sync_vital_sign(vital, None, None, uid='patient')

    async def test_commit_response_lost_retry_does_not_duplicate_water(self):
        vital = self.vital()
        first = await self.upload(vital)
        for _ in range(3):
            self.assertEqual(first, await self.upload(vital))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT sum(hydration_ml) FROM vital_signs'), 250)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM vital_sign_changes'), 1)
        self.state.dispatch_clinical_alert.assert_awaited_once()

    async def test_undo_before_upload_prevents_resurrection(self):
        vital = self.vital()
        await sync.undo_hydration(vital.id, uid='patient')
        await self.upload(vital)
        await self.upload(vital)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT hydration_ml,deleted_at FROM vital_signs')
            self.assertIsNone(row['hydration_ml'])
            self.assertIsNotNone(row['deleted_at'])
        page = await sync.vital_changes('patient', after=0, limit=200, uid='caregiver')
        self.assertIsNotNone(page['changes'][0]['vital']['deleted_at'])

    async def test_undo_mixed_measurement_preserves_pressure(self):
        vital = self.vital(systolicPressure=120, diastolicPressure=80)
        await self.upload(vital)
        await sync.undo_hydration(vital.id, uid='patient')
        await sync.undo_hydration(vital.id, uid='patient')
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM vital_signs')
            self.assertEqual(row['systolic_pressure'], 120)
            self.assertIsNone(row['hydration_ml'])
            self.assertIsNone(row['deleted_at'])

    async def test_delta_includes_late_and_equal_timestamp_new_ids(self):
        stamp = datetime.now(timezone.utc)
        await self.upload(self.vital(measuredAt=stamp.isoformat()))
        first = await sync.vital_changes('patient', after=0, limit=1, uid='patient')
        for time in (stamp, stamp-timedelta(days=1)):
            await self.upload(self.vital(measuredAt=time.isoformat()))
        second = await sync.vital_changes('patient', after=first['nextCursor'], limit=200, uid='patient')
        self.assertEqual(len(second['changes']), 2)
        empty = await sync.vital_changes('patient', after=second['nextCursor'], limit=200, uid='patient')
        self.assertEqual(empty['changes'], [])

    async def test_unrelated_and_revoked_caregiver_cannot_read_or_undo(self):
        vital = self.vital()
        await self.upload(vital)
        with self.assertRaises(HTTPException) as error:
            await sync.undo_hydration(vital.id, uid='caregiver')
        self.assertEqual(error.exception.status_code, 403)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='REVOKED'")
        for uid in ('caregiver', 'stranger'):
            with self.assertRaises(HTTPException):
                await sync.vital_changes('patient', after=0, limit=200, uid=uid)

    async def test_missing_pool_never_confirms_a_measurement(self):
        with patch.object(sync, '_state', SimpleNamespace(db_pool=None)):
            with self.assertRaises(HTTPException) as error:
                await self.upload(self.vital())
        self.assertEqual(error.exception.status_code, 503)

    async def test_caregiver_creation_has_same_patient_id_on_retry_and_list(self):
        request = consultations.CreateConsultation(id=uuid.uuid4(), patientId='patient', title='Cardiologia',
            scheduledDate=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat())
        first = await consultations.create_consultation(request, uid='caregiver')
        self.assertEqual(first, await consultations.create_consultation(request, uid='caregiver'))
        self.assertEqual(first['patientId'], 'patient')
        self.assertEqual(first['createdBy'], 'caregiver')
        self.assertEqual(first['status'], 'ACCEPTED')
        listing = await consultations.list_consultations('patient', uid='patient')
        self.assertEqual([item['id'] for item in listing], [first['id']])
        for target in ('CANCELLED', 'COMPLETED', 'ACCEPTED', 'REJECTED'):
            with self.assertRaises(HTTPException) as error:
                await consultations.transition_consultation(uuid.UUID(first['id']), 'stranger', target)
            self.assertEqual(error.exception.status_code, 403)

    async def test_changes_payload_contains_json_snapshot(self):
        await self.upload(self.vital())
        async with self.pool.acquire() as conn:
            payload = json.loads(await conn.fetchval('SELECT payload FROM vital_sign_changes'))
        self.assertEqual(payload['user_id'], 'patient')

    async def test_fractional_oxygen_and_origin_roundtrip_with_contract_guard(self):
        voice_vital = self.vital(
            hydrationMl=None,
            heartRate=103,
            oxygenSaturation=98.5,
            confirmedVia='braga.voice',
        )
        manual_vital = self.vital(
            hydrationMl=None,
            heartRate=72,
            oxygenSaturation=97.0,
            confirmedVia='braga.manual',
            measuredAt=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        )
        res_voice = await self.upload(voice_vital)
        self.assertEqual(res_voice['vitalsContract'], 2)
        self.assertEqual(res_voice, await self.upload(voice_vital))
        await self.upload(manual_vital)

        # Consumidor legado (v1, sem contract=2) nunca recebe fração que seria truncada por getInt().
        with self.assertRaises(HTTPException) as legacy_list_err:
            await sync.get_vitals('patient', uid='patient', limit=50, offset=0, before=None)
        self.assertEqual(legacy_list_err.exception.status_code, 409)

        with self.assertRaises(HTTPException) as legacy_delta_err:
            await sync.vital_changes('patient', after=0, limit=200, uid='patient')
        self.assertEqual(legacy_delta_err.exception.status_code, 409)

        # Consumidor v2 (contract=2 via query ou header) recebe 98.5 exato, 97 inteiro e confirmed_via preservado.
        rows_v2 = await sync.get_vitals('patient', uid='patient', limit=50, offset=0, before=None, contract=2)
        by_id = {r['id']: r for r in rows_v2}
        self.assertEqual(by_id[str(voice_vital.id)]['oxygen_saturation'], 98.5)
        self.assertIsInstance(by_id[str(voice_vital.id)]['oxygen_saturation'], float)
        self.assertEqual(by_id[str(voice_vital.id)]['heart_rate'], 103)
        self.assertEqual(by_id[str(voice_vital.id)]['confirmed_via'], 'braga.voice')

        self.assertEqual(by_id[str(manual_vital.id)]['oxygen_saturation'], 97)
        self.assertIsInstance(by_id[str(manual_vital.id)]['oxygen_saturation'], int)
        self.assertEqual(by_id[str(manual_vital.id)]['confirmed_via'], 'braga.manual')

        header_req = SimpleNamespace(headers={'x-braga-vitals-contract': '2'})
        delta_v2 = await sync.vital_changes('patient', after=0, limit=200, request=header_req, uid='caregiver')
        self.assertEqual(delta_v2['vitalsContract'], 2)
        delta_by_id = {c['vital']['id']: c['vital'] for c in delta_v2['changes']}
        self.assertEqual(delta_by_id[str(voice_vital.id)]['oxygen_saturation'], 98.5)
        self.assertEqual(delta_by_id[str(voice_vital.id)]['confirmed_via'], 'braga.voice')
        self.assertEqual(delta_by_id[str(manual_vital.id)]['oxygen_saturation'], 97)
        self.assertEqual(delta_by_id[str(manual_vital.id)]['confirmed_via'], 'braga.manual')

    async def test_bounds_and_origin_validation_reject_invalid_readings(self):
        from pydantic import ValidationError
        for invalid_dto in (
            dict(heartRate=72.5),
            dict(oxygenSaturation=float('nan')),
            dict(oxygenSaturation=float('inf')),
            dict(heartRate=True),
        ):
            with self.assertRaises(ValidationError):
                self.vital(**invalid_dto)

        for invalid_upload in (
            dict(heartRate=19),
            dict(heartRate=251),
            dict(oxygenSaturation=29.9),
            dict(oxygenSaturation=100.1),
            dict(confirmedVia='origem.desconhecida'),
        ):
            with self.assertRaises(HTTPException) as err:
                await self.upload(self.vital(**invalid_upload))
            self.assertEqual(err.exception.status_code, 422)
