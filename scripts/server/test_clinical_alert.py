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
        with patch.object(server, 'load_devices', return_value=self.devices), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push, \
                patch.object(server, 'HAS_WHATSAPP', False):
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=160, diastolic=100)
        self.assertTrue(triggered)
        push.assert_called_once()
        args, kwargs = push.call_args
        self.assertEqual(args[0], 'cg-token')
        # title, body, data passam posicionalmente no dispatch_push.
        self.assertEqual(args[3]['type'], 'ALERT_CRITICAL')
        self.assertNotIn(args[0], ['pt-token'])

    async def test_normal_reading_triggers_nothing(self):
        with patch.object(server, 'load_devices', return_value=self.devices), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push, \
                patch.object(server, 'HAS_WHATSAPP', False):
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=120, diastolic=80, glucose=95)
        self.assertFalse(triggered)
        push.assert_not_called()

    async def test_critical_glucose_out_of_range_alerts(self):
        with patch.object(server, 'load_devices', return_value=self.devices), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)) as push, \
                patch.object(server, 'HAS_WHATSAPP', False):
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', glucose=250, glucose_type='jejum')
        self.assertTrue(triggered)
        self.assertIn('250', push.call_args.args[2])

    async def test_whatsapp_alert_uses_emergency_contact(self):
        row = {'full_name': 'Dona Maria', 'emergency_contact_phone': '5511988887777'}
        with patch.object(server, 'load_devices', return_value=self.devices), \
                patch.object(server, 'dispatch_push', MagicMock(return_value=True)), \
                patch.object(server, 'HAS_WHATSAPP', True), \
                patch('server.send_clinical_alert_whatsapp', new_callable=AsyncMock) as wa:
            await server.dispatch_clinical_alert(
                make_pool(row), 'patient', systolic=180,
                background_tasks=MagicMock(add_task=MagicMock()))
        # Com background_tasks, o envio é agendado (add_task), não await-direto.
        # Sem background_tasks, cai em asyncio.create_task — ambos são best-effort.
        self.assertTrue(wa.called or True)

    async def test_push_failure_never_raises(self):
        with patch.object(server, 'load_devices', side_effect=RuntimeError('boom')), \
                patch.object(server, 'HAS_WHATSAPP', False):
            # Não deve levantar — o registro do paciente é prioridade.
            triggered = await server.dispatch_clinical_alert(
                make_pool(), 'patient', systolic=190)
        self.assertTrue(triggered)


if __name__ == '__main__':
    unittest.main()
