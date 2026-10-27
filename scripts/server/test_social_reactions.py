import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import sync


class SocialReactionTests(unittest.TestCase):
    def setUp(self):
        self.post = str(uuid.uuid4())
        self.conn = AsyncMock()
        self.conn.fetchval.return_value = uuid.UUID(self.post)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.state = SimpleNamespace(db_pool=pool)
        patcher = patch.object(sync, "_state", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(sync.router)
        app.dependency_overrides[firebase_uid] = lambda: "reader"
        self.client = TestClient(app)
        self.payload = {"postId": self.post, "userId": "reader", "reactionType": "apoio",
                        "createdAt": "2026-10-27T12:00:00Z"}

    def test_reaction_updates_same_user_and_respects_post_audience(self):
        for kind in ("apoio", "palmas", "forca"):
            response = self.client.post("/api/sync/post-reaction", json={**self.payload, "reactionType": kind})
            self.assertEqual(200, response.status_code)
            sql, post, owner, saved_kind, _ = self.conn.fetchval.call_args.args
            self.assertEqual((uuid.UUID(self.post), "reader", kind), (post, owner, saved_kind))
            self.assertIn("DO UPDATE SET reaction_type", sql)
            self.assertIn("sp.is_visible = TRUE", sql)
            self.assertIn("sp.visibility = 'PUBLIC'", sql)
            self.assertIn("b.status = 'ACTIVE'", sql)

    def test_other_account_cannot_forge_reaction(self):
        response = self.client.post("/api/sync/post-reaction", json={**self.payload, "userId": "other"})
        self.assertEqual(403, response.status_code)
        self.conn.fetchval.assert_not_awaited()

    def test_unavailable_or_private_post_is_not_success(self):
        self.conn.fetchval.return_value = None
        self.assertEqual(404, self.client.post("/api/sync/post-reaction", json=self.payload).status_code)

    def test_offline_database_never_acknowledges_mutations(self):
        self.state.db_pool = None
        self.assertEqual(503, self.client.post("/api/sync/post-reaction", json=self.payload).status_code)
        self.assertEqual(503, self.client.delete(f"/api/social/posts/{self.post}/reaction").status_code)
        self.conn.execute.assert_not_awaited()

    def test_delete_is_repeatable_and_scoped_to_authenticated_user(self):
        for _ in range(2):
            response = self.client.delete(f"/api/social/posts/{self.post}/reaction")
            self.assertEqual({"status": "success"}, response.json())
        sql, post, owner = self.conn.execute.call_args.args
        self.assertIn("post_id = $1 AND user_id = $2", sql)
        self.assertEqual((uuid.UUID(self.post), "reader"), (post, owner))

    def test_account_switch_cannot_remove_new_accounts_reaction(self):
        response = self.client.delete(f"/api/social/posts/{self.post}/reaction?expected_user_id=previous-account")
        self.assertEqual(403, response.status_code)
        self.conn.execute.assert_not_awaited()

    def test_anonymous_cannot_add_or_remove(self):
        self.client.app.dependency_overrides.clear()
        self.assertEqual(401, self.client.post("/api/sync/post-reaction", json=self.payload).status_code)
        self.assertEqual(401, self.client.delete(f"/api/social/posts/{self.post}/reaction").status_code)
        self.conn.execute.assert_not_awaited()
        self.conn.fetchval.assert_not_awaited()

    def test_invalid_id_type_or_date_does_not_write(self):
        for change in ({"postId": "invalid"}, {"reactionType": "unknown"}, {"createdAt": "invalid"}):
            self.assertEqual(422, self.client.post("/api/sync/post-reaction", json={**self.payload, **change}).status_code)
        self.assertEqual(422, self.client.delete("/api/social/posts/invalid/reaction").status_code)
        self.conn.fetchval.assert_not_awaited()

    def test_feed_includes_viewer_reaction_without_exposing_other_readers(self):
        from datetime import datetime, timezone
        self.conn.fetch.return_value = [{"id": uuid.UUID(self.post), "created_at": datetime.now(timezone.utc),
                                         "has_user_reacted": True, "user_reaction_type": "palmas"}]
        response = self.client.get("/api/social/feed")
        self.assertEqual("palmas", response.json()[0]["user_reaction_type"])
        sql, _, _, owner = self.conn.fetch.call_args.args
        self.assertIn("pr.user_id = $3) as user_reaction_type", sql)
        self.assertEqual("reader", owner)
