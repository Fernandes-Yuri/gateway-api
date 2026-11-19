import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from core.b2b_onboarding import (
    STATUS_APPROVED,
    STATUS_CHANGES_REQUESTED,
    STATUS_DRAFT,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    generate_invite_code,
    list_partner_programs_for_admin,
    review_partner_program,
)


class TestB2BManualApproval(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prog_id = "11111111-1111-1111-1111-111111111111"
        self.pending_row = {
            "id": self.prog_id,
            "owner_user_id": "uid-partner",
            "company_name": "Clínica Horizonte Ltda",
            "program_name": "Horizonte Cuidado",
            "cnpj": "12.345.678/0001-95",
            "primary_color": "#1A6B5C",
            "logo_url": None,
            "approval_status": STATUS_PENDING_REVIEW,
            "review_note": None,
            "invite_code": None,
            "invite_paused": False,
            "seat_limit": 10,
        }
        self.conn = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            fetchrow=AsyncMock(return_value=None),
            fetchval=AsyncMock(return_value=0),
            execute=AsyncMock(return_value="INSERT 0 1"),
        )

    def test_generate_invite_code_format_and_prefix(self):
        code = generate_invite_code("Clínica Horizonte")
        self.assertRegex(code, r"^CLIN-[A-Z2-9]{4}$")
        self.assertNotIn("O", code.split("-")[1])
        self.assertNotIn("0", code.split("-")[1])
        self.assertNotIn("I", code.split("-")[1])
        self.assertNotIn("1", code.split("-")[1])

    async def test_approve_generates_invite_code_and_unlocks_promotion(self):
        now = datetime.now(timezone.utc)
        approved_row = dict(
            self.pending_row,
            approval_status=STATUS_APPROVED,
            invite_code="HORI-7K9P",
            reviewed_by="yuri@bragasaude.online",
            reviewed_at=now,
            seat_limit=15,
        )
        self.conn.fetchrow = AsyncMock(side_effect=[self.pending_row, approved_row])
        # 1a fetchval = checagem de colisao do invite_code (None = sem colisao);
        # 2a fetchval = contagem de participantes ativos (0)
        self.conn.fetchval = AsyncMock(side_effect=[None, 0])

        result = await review_partner_program(
            self.conn,
            program_id=self.prog_id,
            action="approve",
            reviewer="yuri@bragasaude.online",
            note="Documentação validada.",
            seat_limit=15,
        )
        self.assertEqual(result["approvalStatus"], STATUS_APPROVED)
        self.assertEqual(result["inviteCode"], "HORI-7K9P")
        self.assertEqual(result["seatLimit"], 15)
        self.assertTrue(result["canPromote"])
        self.conn.execute.assert_awaited_once()

    async def test_request_changes_and_reject_require_note(self):
        self.conn.fetchrow = AsyncMock(return_value=self.pending_row)
        with self.assertRaises(HTTPException) as ctx:
            await review_partner_program(
                self.conn,
                program_id=self.prog_id,
                action="request_changes",
                reviewer="yuri@bragasaude.online",
                note="   ",
            )
        self.assertEqual(ctx.exception.status_code, 400)

        now = datetime.now(timezone.utc)
        changes_row = dict(
            self.pending_row,
            approval_status=STATUS_CHANGES_REQUESTED,
            review_note="Confirmar CNPJ da matriz.",
            reviewed_by="yuri@bragasaude.online",
            reviewed_at=now,
        )
        self.conn.fetchrow = AsyncMock(side_effect=[self.pending_row, changes_row])
        res_changes = await review_partner_program(
            self.conn,
            program_id=self.prog_id,
            action="request_changes",
            reviewer="yuri@bragasaude.online",
            note="Confirmar CNPJ da matriz.",
        )
        self.assertEqual(res_changes["approvalStatus"], STATUS_CHANGES_REQUESTED)
        self.assertFalse(res_changes["canPromote"])
        self.assertIsNone(res_changes["inviteCode"])

        rejected_row = dict(
            self.pending_row,
            approval_status=STATUS_REJECTED,
            review_note="Atividade fora do escopo do piloto.",
            reviewed_by="yuri@bragasaude.online",
            reviewed_at=now,
        )
        self.conn.fetchrow = AsyncMock(side_effect=[self.pending_row, rejected_row])
        res_reject = await review_partner_program(
            self.conn,
            program_id=self.prog_id,
            action="reject",
            reviewer="yuri@bragasaude.online",
            note="Atividade fora do escopo do piloto.",
        )
        self.assertEqual(res_reject["approvalStatus"], STATUS_REJECTED)
        self.assertFalse(res_reject["canPromote"])

    async def test_review_blocks_non_pending_status_with_409(self):
        draft_row = dict(self.pending_row, approval_status=STATUS_DRAFT)
        self.conn.fetchrow = AsyncMock(return_value=draft_row)
        with self.assertRaises(HTTPException) as ctx:
            await review_partner_program(
                self.conn,
                program_id=self.prog_id,
                action="approve",
                reviewer="yuri@bragasaude.online",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_list_partner_programs_for_admin_validates_filter(self):
        with self.assertRaises(HTTPException) as ctx:
            await list_partner_programs_for_admin(self.conn, status="INVALIDO")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
