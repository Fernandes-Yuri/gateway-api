"""
Testes de Regressão e Validação das Otimizações de Query e Custos de Banco (sync.py).
Arquivo: scripts/server/test_query_optimizations.py
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

server_dir = Path(__file__).resolve().parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import sync


class _MockPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _create_test_client(conn, uid: str | None = None):
    app = FastAPI()
    sync.configure_state(db_pool=_MockPool(conn))
    app.include_router(sync.router)
    if uid:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app, raise_server_exceptions=False)


class TestQueryOptimizations(unittest.TestCase):
    def setUp(self):
        self.user_id = "user-perf-001"

    def test_get_medications_includes_limit_clause(self):
        """Garante que a listagem de medicamentos possui teto defensivo LIMIT 100."""
        conn = MagicMock()
        executed_query = None

        async def mock_fetch(query, *args):
            nonlocal executed_query
            executed_query = query
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)
        client = _create_test_client(conn, uid=self.user_id)

        response = client.get(f"/api/medications/{self.user_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(executed_query)
        self.assertIn("LIMIT 100", executed_query)

    def test_get_vitals_with_cursor_omits_offset_when_zero(self):
        """Garante que a paginação de vitais com cursor 'before' não injeta OFFSET redundante quando offset=0."""
        conn = MagicMock()
        executed_query = None

        async def mock_fetch(query, *args):
            nonlocal executed_query
            executed_query = query
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)
        conn.fetchval = AsyncMock(return_value=True)  # require_family_access
        client = _create_test_client(conn, uid=self.user_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        response = client.get(f"/api/vitals/{self.user_id}?before={now_iso}&limit=20&offset=0")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(executed_query)
        self.assertIn("measured_at < $2", executed_query)
        self.assertIn("LIMIT $3", executed_query)
        self.assertNotIn("OFFSET", executed_query)

    def test_get_daily_metrics_with_cursor_omits_offset_when_zero(self):
        """Garante que a paginação de métricas com 'before_date' não injeta OFFSET redundante quando offset=0."""
        conn = MagicMock()
        executed_query = None

        async def mock_fetch(query, *args):
            nonlocal executed_query
            executed_query = query
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)
        conn.fetchval = AsyncMock(return_value=True)  # require_owner_or_caregiver
        client = _create_test_client(conn, uid=self.user_id)

        response = client.get(f"/api/metrics/daily/{self.user_id}?before_date=2026-11-10&days=7&offset=0")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(executed_query)
        self.assertIn("date < $2", executed_query)
        self.assertIn("LIMIT $3", executed_query)
        self.assertNotIn("OFFSET", executed_query)

    def test_social_feed_query_uses_join_aggregation(self):
        """Garante que a query do mural social utiliza LEFT JOIN com GROUP BY em vez de subqueries correlacionadas N+1."""
        conn = MagicMock()
        executed_query = None

        now_dt = datetime.now(timezone.utc)
        mock_post = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "user_id": self.user_id,
            "post_type": "ACHIEVEMENT",
            "title": "Meta atingida",
            "description": "Bebi 2L de água hoje",
            "created_at": now_dt,
            "edited_at": None,
            "visibility": "PUBLIC",
            "user_name": "Paciente Silva",
            "user_nickname": "silva_saude",
            "avatar_identifier": "avatar_1",
            "photo_url": None,
            "reaction_count": 3,
            "has_user_reacted": True,
            "user_reaction_type": "palmas",
        }

        async def mock_fetch(query, *args):
            nonlocal executed_query
            executed_query = query
            return [mock_post]

        conn.fetch = AsyncMock(side_effect=mock_fetch)
        client = _create_test_client(conn, uid=self.user_id)

        response = client.get("/api/social/feed?limit=10&offset=0")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["reaction_count"], 3)
        self.assertTrue(data[0]["has_user_reacted"])
        self.assertEqual(data[0]["user_reaction_type"], "palmas")

        self.assertIsNotNone(executed_query)
        # Verifica a substituição de subquery N+1 por LEFT JOIN com agregação
        self.assertIn("LEFT JOIN (", executed_query)
        self.assertIn("GROUP BY post_id", executed_query)
        self.assertIn("COALESCE(counts.reaction_count, 0)", executed_query)
        self.assertNotIn("(SELECT COUNT(*) FROM post_reactions pr WHERE pr.post_id = sp.id)", executed_query)


if __name__ == "__main__":
    unittest.main()
