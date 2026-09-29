import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import sync
from api_identity import firebase_uid


class CommunityProfileTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = {"community_nickname": "Ana"}
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        state = SimpleNamespace(db_pool=pool, rate_limiter=SimpleNamespace(check=AsyncMock()))
        self.patcher = patch.object(sync, "_state", state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        app = FastAPI()
        app.include_router(sync.router)
        app.dependency_overrides[firebase_uid] = lambda: "conta-autenticada"
        self.client = TestClient(app)

    def test_ignora_identidade_enviada_no_corpo(self):
        response = self.client.post("/api/community/profile", json={"nickname": "Ana", "userId": "outra-conta"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.conn.fetchrow.call_args.args[1:], ("conta-autenticada", "Ana"))

    def test_remove_nome_sem_alterar_nome_civil(self):
        self.conn.fetchrow.return_value = {"community_nickname": None}
        response = self.client.post("/api/community/profile", json={"nickname": ""})
        self.assertEqual(response.json(), {"nickname": ""})
        self.assertEqual(self.conn.fetchrow.call_args.args[1:], ("conta-autenticada", None))
        self.assertNotIn("full_name", self.conn.fetchrow.call_args.args[0])

    def test_entrada_invalida_nao_grava(self):
        response = self.client.post("/api/community/profile", json={"nickname": "<script>"})
        self.assertEqual(response.status_code, 422)
        self.conn.fetchrow.assert_not_called()

    def test_leitura_da_propria_conta(self):
        self.assertEqual(self.client.get("/api/community/profile").json(), {"nickname": "Ana"})
        self.assertEqual(self.conn.fetchrow.call_args.args[1:], ("conta-autenticada",))
