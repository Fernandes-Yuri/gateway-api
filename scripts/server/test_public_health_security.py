"""Regressões do contrato público e das respostas CORS/firewall."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from routers import health


class PublicHealthSecurityTests(unittest.TestCase):
    def setUp(self):
        # Sem lifespan: testes não iniciam banco, scheduler ou integrações.
        self.client = TestClient(server.app)

    def test_health_does_not_disclose_runtime_configuration(self):
        for connected in (False, True):
            with self.subTest(connected=connected), patch.object(
                health._state, 'db_pool', object() if connected else None
            ), patch.object(health._state, 'has_postgres', connected):
                response = self.client.get('/health?debug=true')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'status': 'online'})
                self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_json_success_and_errors_have_security_headers(self):
        for path, status in (('/health', 200), ('/nonexistent-security-test', 404)):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                self.assertEqual(response.headers['x-frame-options'], 'DENY')
                self.assertIn("default-src 'none'", response.headers['content-security-policy'])

    def test_cors_preflight_allows_configured_origin_and_rejects_unknown(self):
        for origin, status in ((server.ALLOWED_ORIGINS[0], 200),
                               ('https://untrusted.invalid', 400)):
            with self.subTest(origin=origin):
                response = self.client.options('/health', headers={
                    'Origin': origin, 'Access-Control-Request-Method': 'GET',
                })
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                if status == 200:
                    self.assertEqual(response.headers['access-control-allow-origin'], origin)
                else:
                    self.assertNotIn('access-control-allow-origin', response.headers)

    def test_unknown_origin_cannot_read_health_via_cors(self):
        response = self.client.get('/health', headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_html_help_keeps_its_content_policy(self):
        response = self.client.get('/socorro')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("default-src 'none'", response.headers.get('content-security-policy', ''))
