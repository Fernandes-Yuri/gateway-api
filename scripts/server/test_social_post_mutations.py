from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api_identity import firebase_uid
from routers import sync

POST_ID = '00000000-0000-0000-0000-000000000001'


class TestSocialPostMutations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = AsyncMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        self.conn.transaction = MagicMock(return_value=transaction)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.app = FastAPI()
        self.app.include_router(sync.router)

        async def author():
            return 'author'

        self.app.dependency_overrides[firebase_uid] = author
        self.state_patch = patch.object(sync, '_state', SimpleNamespace(db_pool=self.pool))
        self.state_patch.start()
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.state_patch.stop()

    async def test_other_author_cannot_mutate_patch(self):
        self.conn.fetchrow.return_value = {'user_id': 'another', 'is_visible': True}
        response = await self.client.patch(
            f'/api/sync/social-post/{POST_ID}', json={'title': 'Novo'}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.conn.fetchrow.await_count, 1)
        self.conn.execute.assert_not_awaited()

    async def test_other_author_cannot_mutate_delete(self):
        self.conn.fetchrow.return_value = {'user_id': 'another', 'is_visible': True}
        response = await self.client.delete(f'/api/sync/social-post/{POST_ID}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.conn.fetchrow.await_count, 1)
        self.conn.execute.assert_not_awaited()

    async def test_author_edit_and_timestamp(self):
        self.conn.fetchrow.side_effect = [
            {'user_id': 'author', 'is_visible': True},
            {'edited_at': datetime(2026, 10, 9, 12, tzinfo=timezone.utc)},
        ]
        response = await self.client.patch(
            f'/api/sync/social-post/{POST_ID}',
            json={'title': ' Novo ', 'description': 'Conteúdo'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['edited_at'], '2026-10-09T12:00:00Z')
        args = self.conn.fetchrow.call_args.args
        self.assertIn('edited_at = NOW()', args[0])
        self.assertEqual(args[2:], ('Novo', 'Conteúdo'))

    async def test_author_soft_delete_is_idempotent(self):
        for visible in (True, False):
            self.conn.fetchrow.return_value = {'user_id': 'author', 'is_visible': visible}
            response = await self.client.delete(f'/api/sync/social-post/{POST_ID}')
            self.assertEqual(response.status_code, 200)
        self.assertIn('is_visible = FALSE', self.conn.execute.call_args.args[0])

    async def test_missing_post_patch(self):
        self.conn.fetchrow.return_value = None
        response = await self.client.patch(
            f'/api/sync/social-post/{POST_ID}', json={'title': 'Novo'}
        )
        self.assertEqual(response.status_code, 404)
        self.conn.execute.assert_not_awaited()

    async def test_missing_post_delete(self):
        self.conn.fetchrow.return_value = None
        response = await self.client.delete(f'/api/sync/social-post/{POST_ID}')
        self.assertEqual(response.status_code, 404)
        self.conn.execute.assert_not_awaited()

    async def test_deleted_post_cannot_be_edited(self):
        self.conn.fetchrow.return_value = {'user_id': 'author', 'is_visible': False}
        response = await self.client.patch(
            f'/api/sync/social-post/{POST_ID}', json={'title': 'Novo'}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.conn.fetchrow.await_count, 1)

    async def test_invalid_edit(self):
        for body in [
            {'title': ''},
            {'title': '  '},
            {'title': 'a' * 201},
            {'description': 'Novo'},
        ]:
            response = await self.client.patch(f'/api/sync/social-post/{POST_ID}', json=body)
            self.assertEqual(response.status_code, 422)
            self.conn.fetchrow.assert_not_awaited()

    async def test_invalid_uuid(self):
        response = await self.client.delete('/api/sync/social-post/not-a-uuid')
        self.assertEqual(response.status_code, 422)
        self.conn.fetchrow.assert_not_awaited()

    async def test_database_unavailable(self):
        with patch.object(sync, '_state', SimpleNamespace(db_pool=None)):
            response = await self.client.delete(f'/api/sync/social-post/{POST_ID}')
            self.assertEqual(response.status_code, 503)

    async def test_feed_serializes_edit_and_null(self):
        now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.conn.fetch.return_value = [
            {'id': POST_ID, 'created_at': now, 'edited_at': now},
            {'id': POST_ID, 'created_at': now, 'edited_at': None},
        ]
        response = await self.client.get('/api/social/feed')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['edited_at'], '2026-10-09T12:00:00Z')
        self.assertIsNone(response.json()[1]['edited_at'])
        self.assertIn('sp.is_visible = TRUE', self.conn.fetch.call_args.args[0])

    async def test_missing_authentication_patch(self):
        self.app.dependency_overrides.clear()
        response = await self.client.patch(
            f'/api/sync/social-post/{POST_ID}', json={'title': 'Novo'}
        )
        self.assertIn(response.status_code, (401, 403))
        self.conn.fetchrow.assert_not_awaited()

    async def test_missing_authentication_delete(self):
        self.app.dependency_overrides.clear()
        response = await self.client.delete(f'/api/sync/social-post/{POST_ID}')
        self.assertIn(response.status_code, (401, 403))
        self.conn.fetchrow.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
