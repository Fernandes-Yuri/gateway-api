import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from core.b2b_onboarding import (
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PENDING_REVIEW,
    lookup_invite_preview,
    normalize_cnpj,
    normalize_primary_color,
    submit_my_partner_program,
    upsert_my_partner_program,
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


class TestB2BOnboarding(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = SimpleNamespace(
            fetchrow=AsyncMock(return_value=None),
            fetchval=AsyncMock(return_value=0),
            execute=AsyncMock(return_value="INSERT 0 1"),
        )
        self.pool = _FakePool(self.conn)
        b2b_router.configure_state(db_pool=self.pool)

    def test_normalize_primary_color_and_cnpj(self):
        self.assertEqual(normalize_primary_color("#1a6b5c"), "#1A6B5C")
        with self.assertRaises(HTTPException):
            normalize_primary_color("javascript:alert(1)")

        self.assertEqual(
            normalize_cnpj("12.345.678/0001-95", required=True),
            "12.345.678/0001-95",
        )
        with self.assertRaises(HTTPException):
            normalize_cnpj("", required=True)
        with self.assertRaises(HTTPException):
            normalize_cnpj("11.111.111/1111-11", required=True)

    async def test_upsert_creates_draft_and_editing_pending_returns_to_draft(self):
        prog_id = "11111111-1111-1111-1111-111111111111"
        base_row = {
            "id": prog_id,
            "owner_user_id": "uid-partner",
            "company_name": "Clínica Horizonte Ltda",
            "program_name": "Horizonte Cuidado",
            "cnpj": "12.345.678/0001-95",
            "primary_color": "#1A6B5C",
            "logo_url": None,
            "approval_status": STATUS_DRAFT,
            "review_note": None,
            "invite_code": "HORI-01",
            "invite_paused": False,
            "seat_limit": 10,
        }
        self.conn.fetchrow = AsyncMock(side_effect=[None, base_row])

        created = await upsert_my_partner_program(
            self.conn,
            "uid-partner",
            company_name="Clínica Horizonte Ltda",
            program_name="Horizonte Cuidado",
            cnpj="12345678000195",
            primary_color="#1a6b5c",
        )
        self.assertEqual(created["approvalStatus"], STATUS_DRAFT)
        self.assertFalse(created["canPromote"])
        self.assertIsNone(created["inviteCode"])

        # Se estava PENDING_REVIEW e o parceiro edita, volta para DRAFT
        self.conn.fetchrow = AsyncMock(
            side_effect=[
                {"id": prog_id, "approval_status": STATUS_PENDING_REVIEW},
                base_row,
            ]
        )
        updated = await upsert_my_partner_program(
            self.conn,
            "uid-partner",
            company_name="Clínica Horizonte Ltda",
            program_name="Horizonte Cuidado+",
            cnpj="12345678000195",
            primary_color="#0F4C81",
        )
        self.assertEqual(updated["approvalStatus"], STATUS_DRAFT)

    async def test_submit_requires_cnpj_and_moves_to_pending_review(self):
        prog_id = "11111111-1111-1111-1111-111111111111"
        draft_without_cnpj = {
            "id": prog_id,
            "owner_user_id": "uid-partner",
            "company_name": "Clínica Horizonte Ltda",
            "program_name": "Horizonte Cuidado",
            "cnpj": None,
            "primary_color": "#1A6B5C",
            "logo_url": None,
            "approval_status": STATUS_DRAFT,
            "review_note": None,
            "invite_code": None,
            "invite_paused": False,
            "seat_limit": 10,
        }
        self.conn.fetchrow = AsyncMock(return_value=draft_without_cnpj)
        with self.assertRaises(HTTPException) as ctx:
            await submit_my_partner_program(self.conn, "uid-partner")
        self.assertEqual(ctx.exception.status_code, 400)

        draft_with_cnpj = dict(draft_without_cnpj, cnpj="12.345.678/0001-95")
        pending_row = dict(draft_with_cnpj, approval_status=STATUS_PENDING_REVIEW)
        self.conn.fetchrow = AsyncMock(side_effect=[draft_with_cnpj, pending_row])

        submitted = await submit_my_partner_program(self.conn, "uid-partner")
        self.assertEqual(submitted["approvalStatus"], STATUS_PENDING_REVIEW)
        self.assertFalse(submitted["canPromote"])

    async def test_lookup_invite_preview_returns_visual_identity_when_approved(self):
        prog_id = "11111111-1111-1111-1111-111111111111"
        self.conn.fetchrow = AsyncMock(
            return_value={
                "id": prog_id,
                "company_name": "Clínica Horizonte",
                "program_name": "Horizonte Cuidado",
                "primary_color": "#1A6B5C",
                "logo_url": "https://cdn.bragasaude.online/logo.webp",
                "invite_code": "HORI-2026",
                "invite_paused": False,
                "seat_limit": 10,
            }
        )
        self.conn.fetchval = AsyncMock(return_value=3)

        preview = await lookup_invite_preview(self.conn, "hori-2026")
        self.assertEqual(preview["programId"], prog_id)
        self.assertEqual(preview["programName"], "Horizonte Cuidado")
        self.assertEqual(preview["primaryColor"], "#1A6B5C")
        self.assertTrue(preview["hasAvailableSeats"])

    async def test_router_save_and_submit_program_use_verified_firebase_uid(self):
        prog_id = "11111111-1111-1111-1111-111111111111"
        row = {
            "id": prog_id,
            "owner_user_id": "uid-jwt",
            "company_name": "Clínica Vida",
            "program_name": "Vida+",
            "cnpj": "12.345.678/0001-95",
            "primary_color": "#1A6B5C",
            "logo_url": None,
            "approval_status": STATUS_APPROVED,
            "review_note": None,
            "invite_code": "VIDA-10",
            "invite_paused": False,
            "seat_limit": 10,
        }
        self.conn.fetchrow = AsyncMock(side_effect=[None, row])
        req = SimpleNamespace(headers={"authorization": "Bearer valid"})
        body = b2b_router.UpsertPartnerProgramRequest(
            companyName="Clínica Vida",
            programName="Vida+",
            cnpj="12345678000195",
            primaryColor="#1A6B5C",
        )
        with patch.object(b2b_router, "firebase_uid", AsyncMock(return_value="uid-jwt")):
            resp = await b2b_router.save_my_program(req, body)

        self.assertEqual(resp["program"]["ownerUserId"], "uid-jwt")
        self.assertEqual(resp["program"]["inviteCode"], "VIDA-10")
        self.assertTrue(resp["program"]["canPromote"])


if __name__ == "__main__":
    unittest.main()
