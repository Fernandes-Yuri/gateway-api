import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import sync
from api_identity import firebase_uid
from social_time import parse_social_timestamp, serialize_social_timestamp


class SocialFeedTimeTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = {"id": "postagem"}
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        patcher = patch.object(sync, "_state", SimpleNamespace(db_pool=pool))
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(sync.router)
        app.dependency_overrides[firebase_uid] = lambda: "usuario"
        self.client = TestClient(app)

    def test_feed_preserva_data_original_em_utc_em_leituras_repetidas(self):
        original = datetime(2026, 9, 15, 12, 30, 0, 123456, tzinfo=timezone(timedelta(hours=-3)))
        self.conn.fetch.return_value = [{"id": "postagem", "created_at": original}]
        for _ in range(2):
            response = self.client.get("/api/social/feed")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()[0]["created_at"], "2026-09-15T15:30:00.123456Z")

    def test_publicacao_preserva_data_enviada_com_offset(self):
        response = self.client.post("/api/sync/social-post", json={
            "userId": "usuario", "postType": "milestone", "title": "Conquista",
            "createdAt": "2026-09-15T12:30:00-03:00"
        })
        self.assertEqual(response.status_code, 200)
        saved = self.conn.fetchrow.call_args.args[5]
        self.assertEqual(saved, datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc))

    def test_data_invalida_nao_e_substituida_pelo_horario_atual(self):
        response = self.client.post("/api/sync/social-post", json={
            "userId": "usuario", "postType": "milestone", "title": "Conquista",
            "createdAt": "data-invalida"
        })
        self.assertEqual(response.status_code, 422)
        self.conn.fetchrow.assert_not_called()

    def test_datas_legadas_sem_fuso_sao_utc(self):
        self.assertEqual(parse_social_timestamp("2026-09-15T15:30:00"),
                         datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(serialize_social_timestamp(datetime(2026, 9, 15, 15, 30)),
                         "2026-09-15T15:30:00Z")
