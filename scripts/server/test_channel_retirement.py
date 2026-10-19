"""Retirada do canal antigo preserva as entradas públicas e do aplicativo."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import server
from routers import auth
from conversation_policy import build_conversation_system


class ChannelRetirementTests(unittest.TestCase):
    def test_old_endpoints_are_not_registered(self):
        client = TestClient(server.app)
        for method, path in [('GET', '/api/whatsapp/status'),
                             ('POST', '/api/whatsapp/send'),
                             ('GET', '/api/whatsapp/webhook'),
                             ('POST', '/api/whatsapp/webhook'),
                             ('GET', '/api/whatsapp/events')]:
            with self.subTest(method=method, path=path):
                self.assertEqual(client.request(method, path).status_code, 404)
        paths = server.app.openapi()['paths']
        self.assertIn('/api/profile/{user_id}', paths)
        self.assertIn('/api/auth/otp/send', paths)

    def test_public_help_and_health_remain_available(self):
        client = TestClient(server.app)
        self.assertEqual(client.get('/socorro').status_code, 200)
        response = client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('whatsapp', response.json())
        self.assertNotIn('has_whatsapp', response.json()['notification_hub'])

    def test_legacy_otp_channel_is_rejected_before_delivery_or_persistence(self):
        pool = MagicMock()
        with patch.object(auth._state, 'db_pool', pool):
            response = TestClient(server.app).post('/api/auth/otp/send', json={
                'identifier': 'synthetic', 'channel': 'WHATSAPP'})
        self.assertEqual(response.status_code, 400)
        pool.acquire.assert_not_called()

    def test_removed_channel_cannot_generate_clinical_conversation(self):
        with self.assertRaises(ValueError):
            build_conversation_system('whatsapp')
        self.assertTrue(build_conversation_system('chat'))
