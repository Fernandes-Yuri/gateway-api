import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import sync
from api_identity import firebase_uid


class AvatarSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = {"photo_url": None}
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        state = SimpleNamespace(db_pool=pool, rate_limiter=SimpleNamespace(check=AsyncMock()))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.moderation = AsyncMock(return_value=(True, ""))
        for patcher in (
            patch.object(sync, "_state", state),
            patch.object(sync, "_avatar_user_dir", return_value=self.directory.name),
            patch.object(sync, "moderate_avatar", self.moderation),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(sync.router)
        app.dependency_overrides[firebase_uid] = lambda: "autor"
        self.client = TestClient(app)

    def test_foto_aprovada_substitui_avatar_no_mesmo_update(self):
        response = self.client.post("/api/profile/autor/avatar", files={"file": ("foto.jpg", b"foto", "image/jpeg")})
        self.assertEqual(response.status_code, 201)
        sql, uid, url = self.conn.execute.call_args.args
        self.assertIn("photo_url = $2, avatar_identifier = NULL", sql)
        self.assertEqual(uid, "autor")
        self.assertEqual(url, response.json()["photo_url"])

    def test_remocao_limpa_selecao_anterior(self):
        self.assertEqual(self.client.delete("/api/profile/autor/avatar").status_code, 200)
        self.assertIn("photo_url = NULL, avatar_identifier = NULL", self.conn.execute.call_args.args[0])

    def test_reprovacao_preserva_foto_e_avatar(self):
        self.moderation.return_value = (False, "Foto não aprovada")
        response = self.client.post("/api/profile/autor/avatar", files={"file": ("foto.jpg", b"foto", "image/jpeg")})
        self.assertEqual(response.status_code, 422)
        self.conn.execute.assert_not_called()

    def test_nao_troca_avatar_de_outra_conta(self):
        response = self.client.post("/api/profile/outro/avatar", files={"file": ("foto.jpg", b"foto", "image/jpeg")})
        self.assertEqual(response.status_code, 403)
        self.moderation.assert_not_called()
        self.conn.execute.assert_not_called()
