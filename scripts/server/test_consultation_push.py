"""Testes locais do push de consulta (doc 10 §2.1/§2.2). Não enviam push real."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api_identity import firebase_uid
from family_api import install_family_routes
from test_alignment import namespace


class ConsultationPushTest(unittest.TestCase):
    def setUp(self):
        self.future = datetime.now(timezone.utc) + timedelta(days=2)
        self.consultation = {
            'id': uuid.UUID(int=1),
            'user_id': 'patient',
            'title': 'Cardiologista',
            'scheduled_date': self.future,
            'status': 'SUGGESTED',
            'caregiver_id': None,
        }
        self.conn = AsyncMock()
        # transaction() deve ser um gerente de contexto assíncrono (AsyncMock
        # puro devolveria uma coroutine, que não suporta 'async with').
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.conn.fetch.return_value = [{'caregiver_id': 'caregiver'}]
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.dispatch = MagicMock(return_value=True)
        self.devices = {'patient': {'token': 'patient-token'}, 'caregiver': {'token': 'caregiver-token'}}
        self.app = FastAPI()
        install_family_routes(self.app, lambda: self.pool, namespace['FamilyBindingSyncDto'],
                              namespace['FamilyMessageSyncDto'],
                              load_devices=lambda: self.devices, dispatch_push=self.dispatch)
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        self.client = TestClient(self.app)
        self.cid = str(uuid.UUID(int=1))

    def _select_then_update(self, new_status):
        # 1a chamada: SELECT ... FOR UPDATE. 2a: UPDATE ... RETURNING *.
        self.conn.fetchrow.side_effect = [
            dict(self.consultation),
            dict(self.consultation, status=new_status, caregiver_id='caregiver'),
        ]

    def _accept_body(self):
        return {'consultationId': self.cid, 'caregiverName': 'Ana', 'caregiverRelation': 'filha'}

    def test_accept_returns_new_status_and_notifies_patient(self):
        # Antes do fix, accept/rejeitar davam NameError (HTTP 500) e devolviam o
        # status antigo. Agora: 200, status ACCEPTED e push ao paciente.
        self._select_then_update('ACCEPTED')
        result = self.client.post('/api/consultation/accept', json=self._accept_body())
        self.assertEqual(200, result.status_code)
        self.assertEqual(result.json()['status'], 'ACCEPTED')
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.kwargs['token'], 'patient-token')
        self.assertEqual(self.dispatch.call_args.kwargs['data']['type'], 'CONSULTATION_ACCEPTED')

    def test_reject_notifies_patient(self):
        self._select_then_update('REJECTED')
        result = self.client.post('/api/consultation/reject', json={'consultationId': self.cid})
        self.assertEqual(200, result.status_code)
        self.assertEqual(result.json()['status'], 'REJECTED')
        self.assertEqual(self.dispatch.call_args.kwargs['data']['type'], 'CONSULTATION_REJECTED')

    def test_patient_cannot_accept_own_suggestion(self):
        self._select_then_update('ACCEPTED')
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        result = self.client.post('/api/consultation/accept', json=self._accept_body())
        self.assertEqual(409, result.status_code)
        self.dispatch.assert_not_called()

    def test_create_notifies_active_caregivers(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        self.conn.fetchrow.return_value = dict(self.consultation)
        body = {'title': 'Cardiologista', 'scheduledDate': self.future.isoformat()}
        result = self.client.post('/api/consultation/create', json=body)
        self.assertEqual(200, result.status_code)
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.kwargs['token'], 'caregiver-token')
        self.assertEqual(self.dispatch.call_args.kwargs['data']['type'], 'CONSULTATION_CREATED')

    def test_cancel_notifies_and_does_not_throw(self):
        self._select_then_update('CANCELLED')
        result = self.client.post('/api/consultation/cancel', json={'consultationId': self.cid})
        self.assertEqual(200, result.status_code)
        self.assertEqual(result.json()['status'], 'CANCELLED')
        self.assertEqual(self.dispatch.call_args.kwargs['data']['type'], 'CONSULTATION_CANCELLED')


if __name__ == '__main__':
    unittest.main()
