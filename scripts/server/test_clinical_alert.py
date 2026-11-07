"""Testes do gatilho clínico compartilhado (doc 10 §3.2).

Garante a paridade: uma leitura crítica vinda do app ou confirmada pelo bot do
WhatsApp percorre exatamente o mesmo caminho de alerta ao cuidador.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server


def make_pool(row=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row or {})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


class DispatchClinicalAlertTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Dois aparelhos: o cuidador e o próprio paciente (nunca deve receber).
        self.devices = {
            'caregiver': {'token': 'cg-token', 'role': 'CAREGIVER'},
            'patient': {'token': 'pt-token', 'role': 'USER'},
        }

    async def test_critical_pressure_notifies_only_caregiver_devices(self):
        with patch.object(server, 'get_active_tokens_for_patient_caregivers', AsyncMock(return_value=['cg-token'])), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push:
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=160, diastolic=100)
        self.assertTrue(triggered)
        push.assert_called_once()
        args, kwargs = push.call_args
        self.assertEqual(args[0], 'cg-token')
        # title, body, data passam posicionalmente no dispatch_push.
        self.assertEqual(args[3]['type'], 'ALERT_CRITICAL')
        self.assertNotIn(args[0], ['pt-token'])
        self.assertIs(kwargs['messaging'], server.messaging)
        self.assertEqual(kwargs['has_fcm'], server.HAS_FCM)
        self.assertFalse(kwargs['include_notification'])
        self.assertEqual(args[3]['body'], args[2])

    async def test_normal_reading_triggers_nothing(self):
        with patch.object(server, 'get_active_tokens_for_patient_caregivers', AsyncMock(return_value=['cg-token'])), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push:
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=120, diastolic=80, glucose=95)
        self.assertFalse(triggered)
        push.assert_not_called()

    async def test_critical_glucose_out_of_range_alerts(self):
        with patch.object(server, 'get_active_tokens_for_patient_caregivers', AsyncMock(return_value=['cg-token'])), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push:
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', glucose=250, glucose_type='jejum')
        self.assertTrue(triggered)
        self.assertIn('250', push.call_args.args[2])


    async def test_push_failure_never_raises(self):
        with patch.object(server, 'get_active_tokens_for_patient_caregivers', AsyncMock(side_effect=RuntimeError('boom'))):
            # Não deve levantar — o registro do paciente é prioridade.
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=190)
        self.assertTrue(triggered)

    async def test_empty_authorized_targets_never_fall_back_to_global_devices(self):
        with patch.object(server, 'get_active_tokens_for_patient_caregivers', AsyncMock(return_value=[])) as targets, \
                patch.object(server, 'load_devices', return_value=self.devices) as legacy, \
                patch.object(server, 'dispatch_push') as push:
            await server.dispatch_clinical_alert(make_pool(), 'patient', systolic=160)
        targets.assert_awaited_once()
        self.assertEqual(targets.call_args.args[1], 'patient')
        legacy.assert_not_called()
        push.assert_not_called()

    async def test_vitals_database_outage_is_not_an_empty_history(self):
        with patch.object(server.sync_router._state, 'db_pool', None):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                await server.sync_router.get_vitals('patient', uid='patient', limit=200, offset=0, before=None)
            self.assertEqual(error.exception.status_code, 503)

    async def test_no_database_never_sends_unverified_alert(self):
        with patch.object(server, 'load_devices', return_value=self.devices) as legacy, \
                patch.object(server, 'dispatch_push') as push:
            self.assertTrue(await server.dispatch_clinical_alert(None, 'patient', systolic=160))
        legacy.assert_not_called()
        push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
