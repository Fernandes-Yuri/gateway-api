"""Regressões de OTP, recuperação e consulta sem exclusão implícita."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
from firebase_admin import auth as fb_auth
from routers import auth, sync


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class AuthAccountLifecycle(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = MagicMock(fetchval=AsyncMock(return_value=False), fetchrow=AsyncMock(return_value=None), execute=AsyncMock())
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.mail = AsyncMock(return_value={"success": True})
        self.user = SimpleNamespace(uid="owner", provider_data=[])
        self.patches = [
            patch.object(auth._state, "db_pool", Pool(self.conn)),
            patch.object(auth._state, "has_email", True),
            patch.object(auth._state, "is_email_configured", return_value=True),
            patch.object(auth._state, "send_security_code_email", self.mail),
            patch.object(auth._state, "send_security_alert_email", AsyncMock()),
            patch.object(auth._state, "has_fcm", True),
            patch.object(auth._state, "firebase_admin", SimpleNamespace(_apps={"test": object()})),
            patch.object(auth._state, "rate_limiter", MagicMock(check=AsyncMock())),
            patch.object(auth._state, "verify_recaptcha", return_value={"valid": True}),
            patch.object(auth, "get_client_ip", return_value="test-ip"),
            patch.object(fb_auth, "get_user_by_email", return_value=self.user),
            patch.object(fb_auth, "update_user"),
            patch.object(fb_auth, "delete_user"),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.lookup, self.update, self.delete = self.mocks[-3:]
        for p in self.patches:
            self.addCleanup(p.stop)

    async def send(self, purpose):
        return await auth.send_auth_otp(MagicMock(), auth.OtpSendRequest(identifier=" Owner@Example.test ", purpose=purpose), BackgroundTasks())

    async def test_existing_firebase_account_cannot_receive_registration_code(self):
        with self.assertRaises(HTTPException) as error:
            await self.send("REGISTRATION")
        self.assertEqual(error.exception.status_code, 409)
        self.mail.assert_not_awaited()
        self.conn.execute.assert_not_awaited()

    async def test_legacy_profile_also_blocks_registration(self):
        self.lookup.side_effect = fb_auth.UserNotFoundError("synthetic missing")
        self.conn.fetchval.return_value = True
        with self.assertRaises(HTTPException) as error:
            await self.send("REGISTRATION")
        self.assertEqual(error.exception.status_code, 409)
        self.mail.assert_not_awaited()

    async def test_missing_account_only_receives_registration_not_reset(self):
        self.lookup.side_effect = fb_auth.UserNotFoundError("synthetic missing")
        with self.assertRaises(HTTPException) as error:
            await self.send("PASSWORD_RESET")
        self.assertEqual(error.exception.status_code, 404)
        self.mail.assert_not_awaited()
        self.conn.execute.assert_not_awaited()
        self.assertEqual((await self.send("REGISTRATION"))["status"], "code_dispatched")
        self.mail.assert_awaited_once()

    async def test_existing_account_receives_reset_code(self):
        self.assertEqual((await self.send("PASSWORD_RESET"))["status"], "code_dispatched")
        self.lookup.assert_called_once_with("owner@example.test")
        self.mail.assert_awaited_once()

    async def test_firebase_error_is_unavailable_and_never_treated_as_missing(self):
        self.lookup.side_effect = RuntimeError("synthetic outage")
        for purpose in ("REGISTRATION", "PASSWORD_RESET"):
            with self.assertRaises(HTTPException) as error:
                await self.send(purpose)
            self.assertEqual(error.exception.status_code, 503)
        self.mail.assert_not_awaited()
        self.conn.execute.assert_not_awaited()

    async def test_unconfigured_auth_does_not_dispatch_or_claim_reset_success(self):
        with patch.object(auth._state, "has_fcm", False):
            with self.assertRaises(HTTPException) as error:
                await self.send("PASSWORD_RESET")
            self.assertEqual(error.exception.status_code, 503)
        self.mail.assert_not_awaited()

    async def test_check_account_preserves_firebase_only_direct_and_google_users(self):
        for provider in ([], [SimpleNamespace(provider_id="google.com")]):
            self.user.provider_data = provider
            result = await auth.check_account_status(MagicMock(), auth.CheckAccountRequest(identifier="owner@example.test"))
            self.assertTrue(result["exists"])
            self.assertFalse(result["canCreateDirect"])
            self.assertEqual(result["provider"], "GOOGLE" if provider else "DIRECT")
        self.delete.assert_not_called()

    async def test_reset_reports_removed_account_without_consuming_token(self):
        self.conn.fetchrow.return_value = {"id": "otp", "created_at": datetime.now(), "expires_at": datetime.now() + timedelta(minutes=10)}
        self.lookup.side_effect = fb_auth.UserNotFoundError("synthetic missing")
        with self.assertRaises(HTTPException) as error:
            await auth.reset_password(auth.PasswordResetRequest(identifier="owner@example.test", resetToken="synthetic-token", newPassword="test-password"), MagicMock())
        self.assertEqual(error.exception.status_code, 404)
        self.conn.execute.assert_not_awaited()
        self.update.assert_not_called()

    async def test_reset_success_requires_firebase_update_and_preserves_password_spaces(self):
        self.conn.fetchrow.return_value = {"id": "otp"}
        result = await auth.reset_password(auth.PasswordResetRequest(identifier="owner@example.test", resetToken="synthetic-token", newPassword=" test-password "), MagicMock())
        self.assertTrue(result["firebaseUpdated"])
        self.update.assert_called_once_with("owner", password=" test-password ")

    async def test_feed_database_outage_is_not_a_confirmed_empty_feed(self):
        with patch.object(sync._state, "db_pool", None):
            with self.assertRaises(HTTPException) as error:
                await sync.get_social_feed(limit=20, offset=0, uid="owner")
            self.assertEqual(error.exception.status_code, 503)
