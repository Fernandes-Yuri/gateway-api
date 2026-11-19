import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from core.b2b_context import (
    MODE_PARTNER_OWNER,
    MODE_PARTNER_PARTICIPANT,
    MODE_PERSONAL,
    list_user_contexts,
    resolve_program_context,
    resolve_request_context,
)
from routers import b2b_context as b2b_router


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class TestB2BContextResolution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            fetchrow=AsyncMock(return_value=None),
            execute=AsyncMock(return_value="INSERT 0 1"),
        )
        self.pool = _FakePool(self.conn)
        b2b_router.configure_state(db_pool=self.pool)

    async def test_list_user_contexts_always_starts_with_personal(self):
        owner_prog_id = "11111111-1111-1111-1111-111111111111"
        part_prog_id = "22222222-2222-2222-2222-222222222222"
        self.conn.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "id": owner_prog_id,
                        "program_name": "Clínica Vida",
                        "company_name": "Clínica Vida Ltda",
                        "primary_color": "#1A6B5C",
                        "logo_url": None,
                        "approval_status": "APPROVED",
                    }
                ],
                [
                    {
                        "id": part_prog_id,
                        "program_name": "Cardio Acompanha",
                        "company_name": "Cardio Sul",
                        "primary_color": "#0F4C81",
                        "logo_url": "https://cdn.bragasaude.online/logo.webp",
                        "approval_status": "APPROVED",
                        "shared_categories": '["steps", "hydration"]',
                    }
                ],
            ]
        )

        contexts = await list_user_contexts(self.conn, "uid-123")
        self.assertEqual(len(contexts), 3)
        self.assertEqual(contexts[0]["mode"], MODE_PERSONAL)
        self.assertIsNone(contexts[0]["programId"])
        self.assertEqual(contexts[1]["mode"], MODE_PARTNER_OWNER)
        self.assertEqual(contexts[1]["programId"], owner_prog_id)
        self.assertEqual(contexts[2]["mode"], MODE_PARTNER_PARTICIPANT)
        self.assertEqual(contexts[2]["programId"], part_prog_id)
        self.assertEqual(contexts[2]["sharedCategories"], ["steps", "hydration"])

    async def test_resolve_program_context_rejects_unauthorized_user_with_403(self):
        self.conn.fetchrow = AsyncMock(return_value=None)
        with self.assertRaises(HTTPException) as ctx:
            await resolve_program_context(
                self.conn,
                "uid-intruso",
                "11111111-1111-1111-1111-111111111111",
                requested_mode=MODE_PARTNER_OWNER,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_resolve_request_context_defaults_to_personal_without_db_hit(self):
        req = SimpleNamespace(
            headers={
                "x-user-id": "spoofed-uid",
                "x-role": "ADMIN",
                "x-context-mode": "PARTNER_OWNER",
            }
        )
        resolved = await resolve_request_context(req, None, "uid-real-jwt")
        self.assertEqual(resolved["mode"], MODE_PERSONAL)
        self.assertEqual(resolved["userId"], "uid-real-jwt")
        self.assertIsNone(resolved["programId"])

    async def test_select_active_context_ignores_spoofed_headers_and_audits_selection(self):
        prog_id = "11111111-1111-1111-1111-111111111111"
        self.conn.fetchrow = AsyncMock(
            return_value={
                "id": prog_id,
                "program_name": "Clínica Vida",
                "company_name": "Clínica Vida Ltda",
                "primary_color": "#1A6B5C",
                "logo_url": None,
                "approval_status": "APPROVED",
            }
        )
        request = SimpleNamespace(
            headers={
                "authorization": "Bearer valid-jwt",
                "x-user-id": "uid-falsificado",
                "x-role": "PARTNER_OWNER",
            }
        )
        payload = b2b_router.SelectContextRequest(
            mode=MODE_PARTNER_OWNER,
            programId=prog_id,
        )

        with patch.object(b2b_router, "firebase_uid", AsyncMock(return_value="uid-dono-real")):
            result = await b2b_router.select_active_context(request, payload)

        self.assertEqual(result["activeContext"]["userId"], "uid-dono-real")
        self.assertEqual(result["activeContext"]["mode"], MODE_PARTNER_OWNER)
        self.assertEqual(result["activeContext"]["programId"], prog_id)
        self.conn.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
