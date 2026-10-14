"""DT-001: identidade de máquina, conta ausente, falha/retry e contrato HTTP."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from fastapi import HTTPException
from firebase_admin import auth

from routers import admin
from services import firebase_deletion_identity as identity


def request(header=""):
    return Request({"type": "http", "headers": [(b"authorization", header.encode())]})


class DeletionIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_identity_rejected(self):
        with self.assertRaises(HTTPException) as error:
            await identity.require_deletion_worker(request())
        self.assertEqual(error.exception.status_code, 401)

    async def test_validated_google_identity_and_exact_audience_required(self):
        claims = {"email": identity.DELETION_SERVICE_ACCOUNT, "email_verified": True}
        with patch.object(identity.id_token, "verify_oauth2_token", return_value=claims) as verify:
            self.assertEqual(await identity.require_deletion_worker(request("Bearer synthetic")), identity.DELETION_SERVICE_ACCOUNT)
        self.assertEqual(verify.call_args.args[2], identity.DELETION_AUDIENCE)

    async def test_invalid_signature_expiry_or_audience_never_authorized(self):
        with patch.object(identity.id_token, "verify_oauth2_token", side_effect=ValueError("invalid")):
            with self.assertRaises(HTTPException) as error:
                await identity.require_deletion_worker(request("Bearer synthetic"))
        self.assertEqual(error.exception.status_code, 401)

    async def test_different_service_or_unverified_email_rejected(self):
        for claims in ({"email": "other@example.test", "email_verified": True},
                       {"email": identity.DELETION_SERVICE_ACCOUNT, "email_verified": False}):
            with patch.object(identity.id_token, "verify_oauth2_token", return_value=claims):
                with self.assertRaises(HTTPException) as error:
                    await identity.require_deletion_worker(request("Bearer synthetic"))
                self.assertEqual(error.exception.status_code, 403)

    async def test_missing_user_allows_cleanup_but_existing_user_does_not(self):
        with patch.object(auth, "get_user", side_effect=auth.UserNotFoundError("synthetic")):
            await identity.require_deleted_firebase_user("synthetic-uid")
        with patch.object(auth, "get_user", return_value=object()):
            with self.assertRaises(HTTPException) as error:
                await identity.require_deleted_firebase_user("synthetic-uid")
        self.assertEqual(error.exception.status_code, 409)

    async def test_firebase_unavailable_never_treated_as_deleted(self):
        with patch.object(auth, "get_user", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(HTTPException) as error:
                await identity.require_deleted_firebase_user("synthetic-uid")
        self.assertEqual(error.exception.status_code, 503)


class DeletionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(admin.router)
        self.client = TestClient(self.app)

    def test_unauthenticated_call_cannot_delete(self):
        with patch("services.account_deletion.delete_account", AsyncMock()) as delete:
            result = self.client.post("/api/admin/firebase-auth-deletion", json={"userId": "synthetic-uid"})
        self.assertEqual(result.status_code, 401)
        delete.assert_not_awaited()

    def test_503_is_retryable_and_success_uses_same_idempotent_deletion_service(self):
        self.app.dependency_overrides[identity.require_deletion_worker] = lambda: identity.DELETION_SERVICE_ACCOUNT
        results = [
            {"success": False, "deleted": True, "storageCleanupPending": True},
            {"success": True, "deleted": True, "storageCleanupPending": False},
            {"success": True, "deleted": True, "storageCleanupPending": False},
        ]
        with patch.object(admin, "_require_pool", return_value=object()), patch.object(
            admin, "require_deleted_firebase_user", AsyncMock()
        ), patch("services.account_deletion.delete_account", AsyncMock(side_effect=results)) as delete:
            codes = [self.client.post("/api/admin/firebase-auth-deletion", json={"userId": "synthetic-uid"}).status_code for _ in results]
        self.assertEqual(codes, [503, 200, 200])
        self.assertEqual(delete.await_count, 3)
        self.assertTrue(all(call.args[1] == "synthetic-uid" for call in delete.await_args_list))

    def test_invalid_uid_rejected_before_cleanup(self):
        self.app.dependency_overrides[identity.require_deletion_worker] = lambda: identity.DELETION_SERVICE_ACCOUNT
        with patch("services.account_deletion.delete_account", AsyncMock()) as delete:
            result = self.client.post("/api/admin/firebase-auth-deletion", json={"userId": "../other"})
        self.assertEqual(result.status_code, 422)
        delete.assert_not_awaited()
