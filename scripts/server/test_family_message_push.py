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

    def test_caregiver_notifies_whole_group_once_except_self(self):
        self.conn.fetch.return_value = [
            {'caregiver_id': 'caregiver'}, {'caregiver_id': 'other-caregiver'},
            {'caregiver_id': 'other-caregiver'}, {'caregiver_id': None}]
        result = self.client.post('/api/sync/family-message', json=self._message('caregiver'))
        self.assertEqual(200, result.status_code)
        self.assertCountEqual(['patient-token', 'never-use'],
                              [call.kwargs['token'] for call in self.dispatch.call_args_list])
        query, patient = self.conn.fetch.call_args.args
        self.assertIn("status='ACTIVE'", query)
        self.assertEqual('patient', patient)
        for call in self.dispatch.call_args_list:
            self.assertEqual('patient', call.kwargs['data']['patientId'])

    def test_revoked_or_unrelated_member_cannot_read_or_send(self):
        self.conn.fetchval.return_value = False
        self.assertEqual(403, self.client.get('/api/family/messages/patient').status_code)
        self.assertEqual(403, self.client.post('/api/sync/family-message',
                         json=self._message('caregiver')).status_code)
        self.conn.fetchrow.assert_not_awaited()
        self.dispatch.assert_not_called()

    def test_spoofed_sender_is_rejected(self):
        self.assertEqual(403, self.client.post('/api/sync/family-message',
                         json=self._message('other-caregiver')).status_code)
        self.conn.fetchrow.assert_not_awaited()

    def test_all_three_members_read_same_patient_scope(self):
        self.conn.fetch.return_value = []
        for member in ['patient', 'caregiver', 'other-caregiver']:
            with self.subTest(member=member):
                self.app.dependency_overrides[firebase_uid] = lambda: member
                self.conn.fetchval.return_value = True
                result = self.client.get('/api/family/messages/patient')
                self.assertEqual(200, result.status_code)
                query, patient, _, _ = self.conn.fetch.call_args.args
                self.assertEqual('patient', patient)
                self.assertIn('patient_id=$1', query)
                self.assertNotIn('sender_id=', query)

    def test_push_failure_never_breaks_the_send(self):
        self.dispatch.side_effect = RuntimeError('FCM indisponível')
        result = self.client.post('/api/sync/family-message', json=self._message('caregiver'))
        self.assertEqual(200, result.status_code)
        self.assertEqual(result.json()['status'], 'success')


if __name__ == '__main__':
    unittest.main()
