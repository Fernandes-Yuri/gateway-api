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


class PublicReleaseCorsTests(unittest.TestCase):
    """A pagina de download le versao/hash do app sem estar na allowlist.

    braga-saude.web.app serve so a pagina de download; se /api/app/latest
    responder sem Access-Control-Allow-Origin, o navegador bloqueia o fetch e
    a pagina publica "Versao indisponivel" em vez do hash real do APK.
    """

    def _client(self):
        from fastapi.testclient import TestClient
        import server
        return TestClient(server.app)

    def _acao(self, path, method="GET", headers=None):
        res = self._client().request(method, path, headers=headers or {})
        return res.headers.get("access-control-allow-origin")

    def test_release_endpoints_allow_any_origin(self):
        for path in ("/api/app/latest", "/api/app/version", "/api/app/download"):
            with self.subTest(path=path):
                self.assertEqual("*", self._acao(path))

    def test_preflight_from_retired_host_is_accepted(self):
        res = self._client().request(
            "OPTIONS", "/api/app/latest",
            headers={"origin": "https://braga-saude.web.app",
                     "access-control-request-method": "GET",
                     "access-control-request-headers": "cache-control"})
        self.assertIn(res.status_code, (200, 204))
        self.assertEqual("*", res.headers.get("access-control-allow-origin"))

    def test_rest_of_api_stays_behind_allowlist(self):
        # Caminho qualquer fora da distribuição: a origem aposentada continua
        # bloqueada — o wildcard não vaza para o resto da API.
        self.assertIsNone(self._acao("/api/items/1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
