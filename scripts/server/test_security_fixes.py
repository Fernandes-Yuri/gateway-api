"""Testes de segurança e origens do site oficial."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from security_defense import get_client_ip


class FakeRequest:
    """Request minimo para get_client_ip (so usa .client e .headers)."""

    def __init__(self, headers=None, client_host="203.0.113.10"):
        self.headers = headers or {}
        self.client = MagicMock()
        self.client.host = client_host


class GetClientIpTests(unittest.TestCase):
    """get_client_ip nao pode confiar em headers forjaveis.

    Antes dessa correcao, x-forwarded-for era lido diretamente, entao
    qualquer um anulava o rate limit mandando um header falso.
    """

    def test_ignored_forged_forwarded_header(self):
        req = FakeRequest(headers={"x-forwarded-for": "1.2.3.4"})
        self.assertEqual(get_client_ip(req), "203.0.113.10")

    def test_ignored_forged_x_real_ip(self):
        req = FakeRequest(headers={"x-real-ip": "10.0.0.9"})
        self.assertEqual(get_client_ip(req), "203.0.113.10")

    def test_real_connection_ip_used_when_no_headers(self):
        req = FakeRequest()
        self.assertEqual(get_client_ip(req), "203.0.113.10")


class CorsAllowlistTests(unittest.TestCase):
    """A allowlist de CORS precisa cobrir toda origem do frontend."""

    def _origins(self):
        # Importar o server executa a construcao do app; isolar so a lista
        # evita depender de pool de banco ou servicos externos.
        import server
        return server.ALLOWED_ORIGINS

    def test_no_wildcard(self):
        self.assertNotIn("*", self._origins())

    def test_official_origins_only(self):
        origins = self._origins()
        self.assertIn("https://bragasaude.online", origins)
        self.assertNotIn("https://braga-saude.web.app", origins)
        self.assertNotIn("https://braga-saude.firebaseapp.com", origins)
        self.assertNotIn("http://localhost:5173", origins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
