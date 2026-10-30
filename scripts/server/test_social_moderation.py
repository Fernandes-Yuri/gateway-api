import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api_identity import firebase_uid
from routers import admin, sync


class SocialModerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.post = str(uuid.uuid4())
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        state = SimpleNamespace(db_pool=self.pool, rate_limiter=MagicMock(check=AsyncMock()))
        for patcher in (patch.object(sync, '_state', state), patch.object(admin, '_state', state),
                        patch.dict('os.environ', {'SERVICE_TOKEN': 'test-service-only'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = FastAPI()
        self.app.include_router(sync.router)
        self.app.include_router(admin.router)
        self.app.dependency_overrides[firebase_uid] = lambda: 'reader'
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_report_uses_token_author_and_closed_reason_set(self):
        self.conn.fetchrow.return_value = {'user_id': 'author'}
        for reason in ('offensive', 'spam', 'misinformation', 'other'):
            response = await self.client.post(f'/api/social/posts/{self.post}/report', json={
                'reason': reason, 'details': '  Teste  ', 'reported_by': 'forged', 'author_id': 'forged'})
            self.assertEqual(201, response.status_code)
            self.assertEqual({'status': 'reported'}, response.json())
            args = self.conn.execute.call_args.args
            self.assertEqual(('reader', 'author', reason, 'Teste'), args[2:])
        for payload in ({'reason': 'invalid'}, {'reason': 'spam', 'details': 'a' * 2001}, {}):
            self.assertEqual(422, (await self.client.post(f'/api/social/posts/{self.post}/report', json=payload)).status_code)

    async def test_own_missing_and_invisible_post_cannot_be_reported(self):
        for post, code in (({'user_id': 'reader'}, 403), (None, 404)):
            self.conn.fetchrow.return_value = post
            response = await self.client.post(f'/api/social/posts/{self.post}/report', json={'reason': 'spam'})
            self.assertEqual(code, response.status_code)
        self.conn.execute.assert_not_awaited()

    async def test_blocks_validate_target_and_unblock_only_token_owner(self):
        for target in ('reader', 'bad.uid'):
            self.assertEqual(422, (await self.client.post(f'/api/social/users/{target}/block')).status_code)
        self.conn.fetchval.return_value = None
        self.assertEqual(404, (await self.client.post('/api/social/users/missing/block')).status_code)
        self.conn.fetchval.return_value = 'author'
        self.assertEqual(200, (await self.client.post('/api/social/users/author/block')).status_code)
        self.assertEqual(200, (await self.client.delete('/api/social/users/author/block')).status_code)
        self.assertEqual(('reader', 'author'), self.conn.execute.call_args.args[1:])

    async def test_public_endpoints_require_identity_and_database(self):
        paths = [(f'/api/social/posts/{self.post}/report', {'reason': 'spam'}),
                 ('/api/social/users/author/block', None)]
        self.app.dependency_overrides.clear()
        for path, body in paths:
            self.assertIn((await self.client.post(path, json=body)).status_code, (401, 403))
        self.assertIn((await self.client.delete(paths[1][0])).status_code, (401, 403))
        self.app.dependency_overrides[firebase_uid] = lambda: 'reader'
        with patch.object(sync, '_state', SimpleNamespace(db_pool=None)):
            for path, body in paths:
                self.assertEqual(503, (await self.client.post(path, json=body)).status_code)
            self.assertEqual(503, (await self.client.delete(paths[1][0])).status_code)

    async def test_moderation_rejects_account_switch_before_saving(self):
        for method, path, body in [('POST', f'/api/social/posts/{self.post}/report', {'reason': 'spam'}),
                                   ('POST', '/api/social/users/author/block', None),
                                   ('GET', '/api/social/blocks', None)]:
            response = await self.client.request(method, path + '?expected_user_id=previous-account', json=body)
            self.assertEqual(403, response.status_code)
        self.conn.execute.assert_not_awaited()
        self.conn.fetchval.assert_not_awaited()

    async def test_admin_requires_service_identity_and_admin_role(self):
        for method, path, body in [('GET', '/api/admin/social-reports', None),
                                   ('POST', f'/api/admin/social-reports/{self.post}/action', {'action': 'dismiss'})]:
            self.assertEqual(401, (await self.client.request(method, path, json=body)).status_code)
            headers = {'Authorization': 'Bearer test-service-only'}
            self.assertEqual(401, (await self.client.request(method, path, json=body, headers=headers)).status_code)
            with patch.object(admin, '_portal_identity', return_value='viewer@example.test'), \
                    patch.object(admin, 'auth_limit', AsyncMock()):
                self.conn.fetchval.return_value = 'viewer'
                self.assertEqual(403, (await self.client.request(method, path, json=body, headers=headers)).status_code)
        self.conn.execute.assert_not_awaited()

    async def test_admin_pagination_action_validation_and_conflict(self):
        headers = {'Authorization': 'Bearer test-service-only'}
        with patch.object(admin, '_moderation_operator', AsyncMock(return_value='admin@example.test')):
            self.conn.fetch.return_value = [{'id': self.post}, {'id': str(uuid.uuid4())}]
            result = await self.client.get('/api/admin/social-reports?limit=1', headers=headers)
            self.assertTrue(result.json()['has_more'])
            self.assertEqual(1, len(result.json()['data']))
            for query in ('status=unknown', 'limit=101', 'offset=-1'):
                self.assertEqual(422, (await self.client.get('/api/admin/social-reports?' + query, headers=headers)).status_code)
            path = f'/api/admin/social-reports/{self.post}/action'
            self.assertEqual(422, (await self.client.post(path, headers=headers, json={'action': 'delete_account'})).status_code)
            self.conn.fetchval.return_value = uuid.UUID(self.post)
            self.conn.fetchrow.return_value = {'author_id': 'author', 'status': 'dismissed'}
            self.assertEqual(200, (await self.client.post(path, headers=headers, json={'action': 'dismiss'})).status_code)
            self.assertEqual(409, (await self.client.post(path, headers=headers, json={'action': 'remove_post'})).status_code)
            self.conn.execute.assert_not_awaited()
