"""Testes locais do push de mensagem familiar (doc 10 §1A). Não enviam push real."""
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api_identity import firebase_uid
from family_api import install_family_routes
from test_alignment import namespace


class FamilyMessagePushTest(unittest.TestCase):
    """Valida a entrega seletiva de push no envio de mensagem familiar."""

    def setUp(self):
        self.conn = AsyncMock()
        # INSERT simulando mensagem nova (ON CONFLICT DO NOTHING RETURNING id).
        self.conn.fetchrow.return_value = {'id': uuid.UUID(int=1)}
        self.conn.fetch.return_value = []
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.dispatch = MagicMock(return_value=True)
        self.devices = {
            'patient': {'token': 'patient-token'},
            'caregiver': {'token': 'caregiver-token'},
            'other-caregiver': {'token': 'never-use'},
        }
        self.app = FastAPI()
        install_family_routes(self.app, lambda: self.pool, namespace['FamilyBindingSyncDto'],
                              namespace['FamilyMessageSyncDto'],
                              load_devices=lambda: self.devices, dispatch_push=self.dispatch)
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        self.client = TestClient(self.app)
        self.id = str(uuid.uuid4())

    def _message(self, sender_id, patient_id='patient'):
        return dict(id=self.id, patientId=patient_id, senderId=sender_id, senderName='Carlos',
                    messageText='Tome seu remédio da pressão', iconType='LOVE', isRead=False,
                    sentAt=datetime.now(timezone.utc).isoformat())

    def test_caregiver_message_notifies_patient_device(self):
        result = self.client.post('/api/sync/family-message', json=self._message('caregiver'))
        self.assertEqual(200, result.status_code)
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.kwargs['token'], 'patient-token')
        self.assertEqual(self.dispatch.call_args.kwargs['data']['type'], 'FAMILY_MESSAGE')
        self.assertEqual(self.dispatch.call_args.kwargs['title'], 'Mensagem de Carlos')

    def test_patient_message_notifies_active_caregivers(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        # Vínculos ativos do paciente: somente 'caregiver'.
        self.conn.fetch.return_value = [{'caregiver_id': 'caregiver'}]
        result = self.client.post('/api/sync/family-message', json=self._message('patient'))
        self.assertEqual(200, result.status_code)
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.kwargs['token'], 'caregiver-token')

    def test_idempotent_retry_does_not_notify_again(self):
        # Mesma mensagem já existe (ON CONFLICT não cria linha) — não notifica de novo.
        self.conn.fetchrow.return_value = None
        result = self.client.post('/api/sync/family-message', json=self._message('caregiver'))
        self.assertEqual(200, result.status_code)
        self.dispatch.assert_not_called()

    def test_push_failure_never_breaks_the_send(self):
        self.dispatch.side_effect = RuntimeError('FCM indisponível')
        result = self.client.post('/api/sync/family-message', json=self._message('caregiver'))
        self.assertEqual(200, result.status_code)
        self.assertEqual(result.json()['status'], 'success')


if __name__ == '__main__':
    unittest.main()
