"""Testes locais com serviços falsos; não enviam notificações reais."""
import unittest
from unittest.mock import AsyncMock, Mock
from fastapi import FastAPI, HTTPException
from caregiver_alerts import CaregiverAlert, install_caregiver_alert_routes


class Acquisition:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        pass


class CaregiverAlertsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = Mock(fetchval=AsyncMock(return_value=True), fetch=AsyncMock(return_value=[]))
        self.pool = Mock(acquire=Mock(return_value=Acquisition(self.conn)))
        self.dispatch = Mock(return_value=True)
        app = FastAPI()
        install_caregiver_alert_routes(app, lambda: self.pool,
            lambda: {'caregiver': {'token': 'test-token'}, 'unrelated': {'token': 'never-use'}}, self.dispatch)
        self.endpoint = next(r.endpoint for r in app.routes if r.path == '/api/family/health-alert')
        self.req = CaregiverAlert(patientId='patient', caregiverId='caregiver', message='Teste')

    async def test_only_the_linked_recipient_is_sent(self):
        result = await self.endpoint(self.req, uid='patient')
        self.assertEqual(result['recipients'], 1)
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.kwargs['token'], 'test-token')
        self.conn.fetchval.assert_awaited_once()

    async def test_wrong_owner_and_revoked_binding_do_not_send(self):
        with self.assertRaises(HTTPException) as error:
            await self.endpoint(self.req, uid='other')
        self.assertEqual(error.exception.status_code, 403)
        self.conn.fetchval.return_value = False
        with self.assertRaises(HTTPException):
            await self.endpoint(self.req, uid='patient')
        self.dispatch.assert_not_called()

    async def test_fcm_rejection_is_not_reported_as_success(self):
        self.dispatch.return_value = False
        with self.assertRaises(HTTPException) as error:
            await self.endpoint(self.req, uid='patient')
        self.assertEqual(error.exception.status_code, 502)
