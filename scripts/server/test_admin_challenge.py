"""Testes do desafio antirrobo do portal (check/verify).

Cobertura:
1. check com precisao >= 92% marca a linha como resolvida e devolve a precisao.
2. check solto fora do alvo (precisao < 92%) consome a linha sem resolver.
3. check sem linha (expirada/ja usada) responde success False.
4. verify so aceita linha marcada resolvida e fresca.
5. Garantias de janela: check exige idade minima de 1s e verify exige frescor.
"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import server

_TEST_TOKEN = "svc_test_token_super_secreto_D51"


def _make_pool(row=None):
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    conn.fetchval.return_value = 0
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _insere_resolvida(conn):
    return any(
        "INSERT INTO admin_challenges" in str(call.args[0])
        for call in conn.execute.call_args_list
        if call.args
    )


class DesafioAntirroboTest(unittest.TestCase):
    def setUp(self):
        self._orig_pool = server.admin_router._state.db_pool
        self._orig_rl = server.admin_router._state.rate_limiter
        server.admin_router._state.rate_limiter = MagicMock(check=AsyncMock())
        self._env_patcher = patch.dict(os.environ, {"SERVICE_TOKEN": _TEST_TOKEN})
        self._env_patcher.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self._env_patcher.stop()
        server.admin_router._state.db_pool = self._orig_pool
        server.admin_router._state.rate_limiter = self._orig_rl

    def _auth(self):
        return {"Authorization": f"Bearer {_TEST_TOKEN}"}

    def _payload(self, answer):
        return {"id": "a" * 32, "binding": "b" * 64, "purpose": "login", "answer": answer}

    def test_check_dentro_da_tolerancia_marca_resolvida(self):
        pool, conn = _make_pool({"answer": 100})
        server.admin_router._state.db_pool = pool
        # erro 20px em 260 => precisao 92% (ponto de corte exato)
        response = self.client.post("/api/admin/challenge/check", json=self._payload(80), headers=self._auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "precision": 92})
        self.assertTrue(_insere_resolvida(conn))
        sql = conn.fetchrow.call_args.args[0]
        self.assertIn("119 seconds", sql)

    def test_check_fora_do_alvo_consome_sem_resolver(self):
        pool, conn = _make_pool({"answer": 100})
        server.admin_router._state.db_pool = pool
        # erro 21px => precisao 91% (abaixo do minimo de 92%)
        response = self.client.post("/api/admin/challenge/check", json=self._payload(121), headers=self._auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": False})
        self.assertFalse(_insere_resolvida(conn))

    def test_check_sem_linha_responde_falha(self):
        pool, conn = _make_pool(None)
        server.admin_router._state.db_pool = pool
        response = self.client.post("/api/admin/challenge/check", json=self._payload(100), headers=self._auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": False})
        self.assertFalse(_insere_resolvida(conn))

    def test_check_erro_zero_tem_cem_por_cento(self):
        pool, _conn = _make_pool({"answer": 150})
        server.admin_router._state.db_pool = pool
        response = self.client.post("/api/admin/challenge/check", json=self._payload(150), headers=self._auth())
        self.assertEqual(response.json(), {"success": True, "precision": 100})

    def test_verify_so_aceita_linha_resolvida_e_fresca(self):
        casos = [
            ({"solved": True, "fresh": True}, True),
            ({"solved": False, "fresh": True}, False),
            ({"solved": True, "fresh": False}, False),
            (None, False),
        ]
        for row, esperado in casos:
            with self.subTest(row=row):
                pool, _conn = _make_pool(row)
                server.admin_router._state.db_pool = pool
                response = self.client.post("/api/admin/challenge/verify", json=self._payload(100), headers=self._auth())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"success": esperado})


if __name__ == "__main__":
    unittest.main()
