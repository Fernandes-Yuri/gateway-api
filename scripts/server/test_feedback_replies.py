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

    def test_reply_persists(self):
        req = sync_router.FeedbackReplyCreate(feedbackId=self.fid, userId='user-1',
                                         body='Obrigado! Já estamos vendo isso.')
        with patch.object(sync_router._state, 'db_pool', self.pool), \
                patch.object(sync_router._state, 'rate_limiter', MagicMock(check=AsyncMock())), \
                patch.object(sync_router, 'sanitize_zero_trust_text', lambda t, **kw: t):
            out = self._run(sync_router.create_feedback_reply(req, self.req, uid='admin'))
        self.assertEqual(out['status'], 'success')
        sql, fid, uid, author, body_text = self.conn.fetchrow.call_args.args
        self.assertIn('INSERT INTO feedback_replies', sql)
        self.assertEqual('Obrigado! Já estamos vendo isso.', body_text)
        self.assertEqual('user-1', uid)

    def test_reply_to_foreign_feedback_is_rejected(self):
        self.conn.fetchval.return_value = None  # não pertence ao usuário
        req = sync_router.FeedbackReplyCreate(feedbackId=self.fid, userId='outro', body='tentativa')
        with patch.object(sync_router._state, 'db_pool', self.pool), \
                patch.object(sync_router._state, 'rate_limiter', MagicMock(check=AsyncMock())), \
                patch.object(sync_router, 'sanitize_zero_trust_text', lambda t, **kw: t):
            with self.assertRaises(server.HTTPException) as error:
                self._run(sync_router.create_feedback_reply(req, self.req, uid='admin'))
        self.assertEqual(error.exception.status_code, 404)
        self.conn.fetchrow.assert_not_called()

    def test_malformed_feedback_id_rejected(self):
        req = sync_router.FeedbackReplyCreate(feedbackId='not-a-uuid', userId='u', body='x')
        with patch.object(sync_router._state, 'db_pool', self.pool), \
                patch.object(sync_router._state, 'rate_limiter', MagicMock(check=AsyncMock())), \
                patch.object(sync_router, 'sanitize_zero_trust_text', lambda t, **kw: t):
            with self.assertRaises(server.HTTPException) as error:
                self._run(sync_router.create_feedback_reply(req, self.req, uid='admin'))
        self.assertEqual(error.exception.status_code, 422)

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
