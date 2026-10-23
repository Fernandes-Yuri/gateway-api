"""Conclusão explícita da identidade na exclusão do próprio titular."""
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import BackgroundTasks, HTTPException
from firebase_admin import auth
from routers import sync
from starlette.requests import Request
import user_moderation
from contextlib import asynccontextmanager
from unittest.mock import MagicMock


class AccountDeleteCompletion(unittest.IsolatedAsyncioTestCase):
    async def test_owner_deletion_concludes_auth_and_only_then_schedules_receipt(self):
        pool = object()
        tasks = BackgroundTasks()
        with patch.object(sync._state, 'db_pool', pool), patch('services.account_lifecycle.account_contact', AsyncMock(return_value=('owner@example.test', 'Pessoa'))), patch('services.account_deletion.delete_account', AsyncMock(return_value={'success': True, 'deleted': True})) as delete, patch.object(auth, 'delete_user') as delete_auth:
            result = await sync.delete_profile('owner', tasks, uid='owner')
        self.assertTrue(result['success'])
        delete_auth.assert_called_once_with('owner')
        delete.assert_awaited_once_with(pool, 'owner', actor='próprio titular', receipt_email='owner@example.test', receipt_name='Pessoa')
        self.assertEqual(len(tasks.tasks), 1)

    async def test_foreign_uid_does_not_delete_and_auth_failure_does_not_claim_completion(self):
        with patch.object(auth, 'delete_user') as delete_auth:
            with self.assertRaises(HTTPException) as forbidden:
                await sync.delete_profile('other', BackgroundTasks(), uid='owner')
            self.assertEqual(forbidden.exception.status_code, 403)
        delete_auth.assert_not_called()
        tasks = BackgroundTasks()
        with patch.object(sync._state, 'db_pool', object()), patch('services.account_lifecycle.account_contact', AsyncMock(return_value=('owner@example.test', 'Pessoa'))), patch('services.account_deletion.delete_account', AsyncMock(return_value={'success': True, 'deleted': True})), patch.object(auth, 'delete_user', side_effect=RuntimeError('synthetic outage')):
            with self.assertRaises(HTTPException) as pending:
                await sync.delete_profile('owner', tasks, uid='owner')
            self.assertEqual(pending.exception.status_code, 503)
        self.assertEqual(tasks.tasks, [])


class DeletedSessionGuard(unittest.IsolatedAsyncioTestCase):
    async def test_old_token_cannot_resurrect_profile_but_can_finish_explicit_delete(self):
        conn = MagicMock(fetchrow=AsyncMock(return_value=None), fetchval=AsyncMock())
        conn.fetchval.side_effect = lambda sql, *args: True if "user_deletion_logs" in sql else None
        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn
        def request(method):
            return Request({'type': 'http', 'method': method, 'path': '/api/profile/owner' if method == 'DELETE' else '/api/sync/profile', 'headers': []})
        with patch.object(user_moderation, '_pool_provider', lambda: Pool()):
            with self.assertRaises(HTTPException) as denied:
                await user_moderation.require_not_restricted('owner', request('POST'), auth_time=100)
            self.assertEqual(denied.exception.status_code, 401)
            await user_moderation.require_not_restricted('owner', request('DELETE'), auth_time=100)
