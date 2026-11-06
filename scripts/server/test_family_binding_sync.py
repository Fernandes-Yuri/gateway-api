"""Fase #6: preserva POST /api/sync/family-binding.

O chat familiar e o E2EE migraram em definitivo para o Go braga-chat, mas a
criacao/revogacao de convites (usada pelo app Android) ainda nao tem
equivalente no servico Go. Este teste garante que o endpoint sobrevive no
Python com autenticacao fail-closed e resposta compativel com o cliente.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import sync as sync_router


class FamilyBindingSyncTest(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.rate = MagicMock()
        self.rate.check = AsyncMock()
        sync_router.configure_state(db_pool=self.pool, rate_limiter=self.rate)
        self.app = FastAPI()
        self.app.include_router(sync_router.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        sync_router.configure_state(db_pool=None, rate_limiter=None)

    def _body(self, **kw):
        base = dict(
            id=str(uuid.uuid4()),
            patientId='patient',
            caregiverName='Ana',
            caregiverRelation='filha',
            connectionCode='ABCD1234',
            status='PENDING',
            caregiverRole='CAREGIVER_VIEWER',
            permissions=['care:read'],
            createdAt='2026-11-06T12:00:00Z',
            expiresAt='2026-11-13T12:00:00Z',
        )
        base.update(kw)
        return base

    def test_unauthenticated_is_rejected(self):
        res = self.client.post('/api/sync/family-binding', json=self._body())
        self.assertEqual(401, res.status_code)
        self.rate.check.assert_not_called()

    def test_create_pending_binding_returns_server_id(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        row = dict(id=uuid.uuid4(), patient_id='patient', status='PENDING')
        self.conn.fetchrow.return_value = row
        res = self.client.post('/api/sync/family-binding', json=self._body())
        self.assertEqual(200, res.status_code)
        self.assertEqual(str(row['id']), res.json()['id'])
        self.rate.check.assert_awaited_once()

    def test_revoked_binding_requires_participant(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'intruder'
        self.conn.fetchrow.return_value = dict(
            id=uuid.uuid4(), patient_id='patient', caregiver_id='caregiver')
        res = self.client.post('/api/sync/family-binding', json=self._body(status='REVOKED'))
        self.assertEqual(403, res.status_code)


if __name__ == '__main__':
    unittest.main()
