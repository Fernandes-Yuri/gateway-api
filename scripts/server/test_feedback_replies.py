"""Testes dos endpoints de resposta a feedback (doc 10 §4.1)."""
import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import server
# Fase A passo 5: feedback migrou de server.py para routers/sync.py.
from routers import sync as sync_router


class FeedbackReplyTest(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetchval.return_value = 1  # feedback pertence ao usuário
        self.conn.fetchrow.return_value = {
            'id': 'rp-1', 'created_at': datetime(2026, 9, 18, 12, tzinfo=timezone.utc)}
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.req = MagicMock()
        self.req.client.host = '127.0.0.1'
        self.fid = str(uuid.UUID(int=1))

    def _run(self, coro):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

    def test_legacy_reply_is_closed_for_any_firebase_user(self):
        for uid in ('admin', 'usuario-comum'):
            req = sync_router.FeedbackReplyCreate(feedbackId=self.fid, userId='user-1', body='resposta')
            with self.assertRaises(server.HTTPException) as error:
                self._run(sync_router.create_feedback_reply(req, self.req, uid=uid))
            self.assertEqual(error.exception.status_code, 410)
        self.conn.fetchrow.assert_not_called()

    def test_pull_returns_and_marks_delivered(self):
        self.conn.fetch.return_value = [{
            'id': 'rp-1', 'feedback_id': 'fb-1', 'author': 'Suporte',
            'body': 'resposta', 'created_at': datetime(2026, 9, 18, 12, tzinfo=timezone.utc)}]
        with patch.object(sync_router._state, 'db_pool', self.pool):
            out = self._run(sync_router.list_feedback_replies(uid='user-1'))
        self.assertEqual(1, len(out))
        self.assertEqual('resposta', out[0]['body'])
        # Marca como entregue na mesma chamada (pull incremental).
        self.conn.execute.assert_called_once()
        self.assertIn('delivered_at=NOW()', self.conn.execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
