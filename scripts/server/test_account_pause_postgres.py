"""Pausa, reativação e comprovantes com PostgreSQL efêmero."""
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from starlette.requests import Request
import test_privacy_postgres as privacy
from services import account_lifecycle as lifecycle, account_deletion
import user_moderation
from core.devices import load_devices_by_user, register_device_token


@unittest.skipUnless(privacy.TEST_DSN, 'PostgreSQL efêmero somente no CI')
class AccountPausePostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await privacy.PrivacyPostgres.asyncSetUp(self)
        async with self.pool.acquire() as conn:
            await conn.execute((privacy.ROOT / 'migrations/035_user_restrictions.sql').read_text())
            await conn.execute("INSERT INTO device_push_tokens(token,user_id) VALUES ('patient-token','paciente'),('other-token','cuidador')")
    async def asyncTearDown(self):
        await privacy.PrivacyPostgres.asyncTearDown(self)

    def request(self, method, path):
        return Request({'type': 'http', 'method': method, 'path': path, 'headers': []})

    async def test_pause_preserves_data_blocks_old_session_and_only_fresh_login_reactivates(self):
        with patch('core.devices.load_devices', return_value={}):
            await lifecycle.deactivate_account(self.pool, 'paciente', 'owner@example.test', 'Paciente')
        async with self.pool.acquire() as conn:
            paused = await conn.fetchval("SELECT paused_at FROM profiles WHERE id='paciente'")
            self.assertIsNotNone(paused)
            self.assertFalse(await conn.fetchval("SELECT is_active FROM device_push_tokens WHERE user_id='paciente'"))
            self.assertTrue(await conn.fetchval("SELECT is_active FROM device_push_tokens WHERE user_id='cuidador'"))
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_lifecycle_mail"), 1)
        old_time = int(paused.timestamp())
        with patch.object(user_moderation, '_pool_provider', lambda: self.pool):
            with self.assertRaises(HTTPException) as denied:
                await user_moderation.require_not_restricted('paciente', self.request('GET', '/api/profile/paciente'), auth_time=old_time)
            self.assertEqual(denied.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                await lifecycle.reactivate_account(self.pool, 'paciente', old_time)
            await lifecycle.reactivate_account(self.pool, 'paciente', old_time + 2)
            with self.assertRaises(HTTPException) as still_revoked:
                await user_moderation.require_not_restricted('paciente', self.request('GET', '/api/profile/paciente'), auth_time=old_time)
            self.assertEqual(still_revoked.exception.status_code, 401)
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval("SELECT paused_at FROM profiles WHERE id='paciente'"))
            self.assertFalse(await conn.fetchval("SELECT is_active FROM device_push_tokens WHERE user_id='paciente'"))
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles"), 2)

    async def test_paused_user_cannot_reenable_push_even_with_late_registration(self):
        with patch('core.devices.load_devices', return_value={}):
            await lifecycle.deactivate_account(self.pool, 'paciente')
        await register_device_token(self.pool, 'paciente', 'late-token')
        devices = await load_devices_by_user(self.pool, legacy_loader=lambda: {'paciente': {'token': 'legacy'}})
        self.assertNotIn('paciente', devices)
        self.assertIn('cuidador', devices)

    async def test_deletion_receipt_waits_for_auth_and_files_and_retries_smtp_without_pii_after_success(self):
        with patch.object(account_deletion, 'cleanup_account_files', AsyncMock(return_value=True)):
            await account_deletion.delete_account(self.pool, 'paciente', actor='titular', receipt_email='owner@example.test', receipt_name='Paciente')
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM account_storage_cleanup WHERE user_id='paciente'")
        send = AsyncMock(return_value={'success': True})
        with patch('services.firebase_account_reconciliation.account_exists', AsyncMock(return_value=True)), patch('email_service.send_account_deletion_email', send):
            await lifecycle.process_account_mail(self.pool)
        send.assert_not_awaited()
        with patch('services.firebase_account_reconciliation.account_exists', AsyncMock(return_value=False)), patch('services.firebase_account_reconciliation.cleanup_legacy_firebase_storage', AsyncMock()), patch('email_service.send_account_deletion_email', send):
            await lifecycle.process_account_mail(self.pool)
            await lifecycle.process_account_mail(self.pool)
        send.assert_awaited_once_with('owner@example.test', 'Paciente')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_lifecycle_mail"), 0)
            self.assertIsNotNone(await conn.fetchval("SELECT confirmation_sent_at FROM user_deletion_logs WHERE user_id='paciente'"))

    async def test_pause_mail_failure_remains_pending_and_retry_succeeds(self):
        with patch('core.devices.load_devices', return_value={}):
            await lifecycle.deactivate_account(self.pool, 'paciente', 'owner@example.test')
            await lifecycle.deactivate_account(self.pool, 'paciente', 'owner@example.test')
        with patch('email_service.send_account_deactivation_email', AsyncMock(return_value={'success': False})):
            await lifecycle.process_account_mail(self.pool)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT attempts FROM account_lifecycle_mail"), 1)
            await conn.execute("UPDATE account_lifecycle_mail SET next_attempt_at=NOW()")
        with patch('email_service.send_account_deactivation_email', AsyncMock(return_value={'success': True})):
            await lifecycle.process_account_mail(self.pool)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_lifecycle_mail"), 0)
