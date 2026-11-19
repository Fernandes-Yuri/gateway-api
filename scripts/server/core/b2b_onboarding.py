"""Onboarding empresarial enxuto, personalizacao e previa de convite (B2B-07).

Regras de produto e seguranca:
- 1 programa por conta responsavel no piloto (`owner_user_id = uid`).
- Identidade vem exclusivamente do JWT RS256 validado (`firebase_uid`).
- Estados de aprovacao (`partner_programs.approval_status`):
  - DRAFT ("Em preparação")
  - PENDING_REVIEW ("Em análise")
  - CHANGES_REQUESTED ("Ajustes solicitados")
  - APPROVED ("Empresa aprovada")
  - REJECTED ("Cadastro recusado")
- Editar um programa em PENDING_REVIEW ou CHANGES_REQUESTED devolve o status a
  DRAFT ate novo envio explicito para analise (conforme REGRAS-EM-DEBATE.md).
- Ajustar identidade visual (nome do programa, cor, logo) de empresa ja
  APPROVED mantem a aprovacao.
- Lookup de convite (`/api/b2b/invite/{code}`) expoe apenas metadados visuais
  de programas APPROVED, sem dados pessoais ou de participantes.
"""
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException

from core.b2b_context import record_context_audit

STATUS_DRAFT = "DRAFT"
STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_CHANGES_REQUESTED = "CHANGES_REQUESTED"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"

DEFAULT_PRIMARY_COLOR = "#1A6B5C"
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def normalize_primary_color(raw: Optional[str]) -> str:
    value = (raw or DEFAULT_PRIMARY_COLOR).strip().upper()
    if not _HEX_COLOR_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail="Cor principal inválida. Use o formato hexadecimal #RRGGBB.",
        )
    return value


def normalize_cnpj(raw: Optional[str], *, required: bool = False) -> Optional[str]:
    cleaned = (raw or "").strip()
    if not cleaned:
        if required:
            raise HTTPException(
                status_code=400,
                detail="Informe o CNPJ da empresa para enviar a solicitação para análise.",
            )
        return None
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) != 14 or len(set(digits)) == 1:
        raise HTTPException(
            status_code=400,
            detail="CNPJ inválido. Informe os 14 dígitos numéricos.",
        )
    return (
        f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/"
        f"{digits[8:12]}-{digits[12:14]}"
    )


def normalize_logo_url(raw: Optional[str]) -> Optional[str]:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > 512 or not cleaned.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="A URL da logo deve utilizar HTTPS.",
        )
    return cleaned


def _format_program_row(row: Any, active_seats: int = 0) -> Dict[str, Any]:
    seat_limit = int(row["seat_limit"] or 10)
    status = row["approval_status"]
    invite_paused = bool(row["invite_paused"])
    return {
        "programId": str(row["id"]),
        "ownerUserId": row["owner_user_id"],
        "companyName": row["company_name"],
        "programName": row["program_name"],
        "cnpj": row["cnpj"],
        "primaryColor": row["primary_color"],
        "logoUrl": row["logo_url"],
        "approvalStatus": status,
        "reviewNote": row["review_note"],
        "inviteCode": row["invite_code"] if status == STATUS_APPROVED else None,
        "invitePaused": invite_paused,
        "seatLimit": seat_limit,
        "activeParticipants": int(active_seats),
        "canPromote": status == STATUS_APPROVED and not invite_paused,
    }


async def _count_active_participants(conn: Any, program_id: str) -> int:
    count = await conn.fetchval(
        """
        SELECT COUNT(*)
          FROM program_enrollments
         WHERE program_id = $1::uuid
           AND status = 'ACTIVE'
        """,
        program_id,
    )
    return int(count or 0)


async def get_my_partner_program(conn: Any, uid: str) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT id::text AS id,
               owner_user_id,
               company_name,
               program_name,
               cnpj,
               primary_color,
               logo_url,
               approval_status,
               review_note,
               invite_code,
               invite_paused,
               seat_limit
          FROM partner_programs
         WHERE owner_user_id = $1
        """,
        uid,
    )
    if not row:
        return None
    active = await _count_active_participants(conn, str(row["id"]))
    return _format_program_row(row, active)


async def upsert_my_partner_program(
    conn: Any,
    uid: str,
    *,
    company_name: str,
    program_name: str,
    cnpj: Optional[str] = None,
    primary_color: Optional[str] = None,
    logo_url: Optional[str] = None,
) -> Dict[str, Any]:
    clean_company = (company_name or "").strip()
    clean_program = (program_name or "").strip()
    if len(clean_company) < 2 or len(clean_company) > 160:
        raise HTTPException(
            status_code=400,
            detail="Informe o nome da empresa (entre 2 e 160 caracteres).",
        )
    if len(clean_program) < 2 or len(clean_program) > 120:
        raise HTTPException(
            status_code=400,
            detail="Informe o nome do programa (entre 2 e 120 caracteres).",
        )

    clean_color = normalize_primary_color(primary_color)
    clean_cnpj = normalize_cnpj(cnpj, required=False)
    clean_logo = normalize_logo_url(logo_url)

    existing = await conn.fetchrow(
        """
        SELECT id::text AS id,
               approval_status
          FROM partner_programs
         WHERE owner_user_id = $1
        """,
        uid,
    )

    if not existing:
        row = await conn.fetchrow(
            """
            INSERT INTO partner_programs (
                owner_user_id,
                company_name,
                program_name,
                cnpj,
                primary_color,
                logo_url,
                approval_status
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'DRAFT')
            RETURNING id::text AS id,
                      owner_user_id,
                      company_name,
                      program_name,
                      cnpj,
                      primary_color,
                      logo_url,
                      approval_status,
                      review_note,
                      invite_code,
                      invite_paused,
                      seat_limit
            """,
            uid,
            clean_company,
            clean_program,
            clean_cnpj,
            clean_color,
            clean_logo,
        )
        await record_context_audit(
            conn,
            program_id=str(row["id"]),
            actor_user_id=uid,
            action="PROGRAM_CREATED",
            metadata={"approvalStatus": STATUS_DRAFT},
        )
        return _format_program_row(row, 0)

    current_status = existing["approval_status"]
    next_status = (
        STATUS_APPROVED
        if current_status == STATUS_APPROVED
        else STATUS_DRAFT
    )

    row = await conn.fetchrow(
        """
        UPDATE partner_programs
           SET company_name = $2,
               program_name = $3,
               cnpj = $4,
               primary_color = $5,
               logo_url = $6,
               approval_status = $7,
               updated_at = NOW()
         WHERE owner_user_id = $1
         RETURNING id::text AS id,
                   owner_user_id,
                   company_name,
                   program_name,
                   cnpj,
                   primary_color,
                   logo_url,
                   approval_status,
                   review_note,
                   invite_code,
                   invite_paused,
                   seat_limit
        """,
        uid,
        clean_company,
        clean_program,
        clean_cnpj,
        clean_color,
        clean_logo,
        next_status,
    )
    active = await _count_active_participants(conn, str(row["id"]))
    await record_context_audit(
        conn,
        program_id=str(row["id"]),
        actor_user_id=uid,
        action="PROGRAM_UPDATED",
        metadata={
            "previousStatus": current_status,
            "approvalStatus": next_status,
        },
    )
    return _format_program_row(row, active)


async def submit_my_partner_program(conn: Any, uid: str) -> Dict[str, Any]:
    existing = await conn.fetchrow(
        """
        SELECT id::text AS id,
               owner_user_id,
               company_name,
               program_name,
               cnpj,
               primary_color,
               logo_url,
               approval_status,
               review_note,
               invite_code,
               invite_paused,
               seat_limit
          FROM partner_programs
         WHERE owner_user_id = $1
        """,
        uid,
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail="Prepare e salve os dados do programa antes de enviar para aprovação.",
        )

    if existing["approval_status"] == STATUS_APPROVED:
        active = await _count_active_participants(conn, str(existing["id"]))
        return _format_program_row(existing, active)

    normalize_cnpj(existing["cnpj"], required=True)

    row = await conn.fetchrow(
        """
        UPDATE partner_programs
           SET approval_status = 'PENDING_REVIEW',
               updated_at = NOW()
         WHERE owner_user_id = $1
         RETURNING id::text AS id,
                   owner_user_id,
                   company_name,
                   program_name,
                   cnpj,
                   primary_color,
                   logo_url,
                   approval_status,
                   review_note,
                   invite_code,
                   invite_paused,
                   seat_limit
        """,
        uid,
    )
    active = await _count_active_participants(conn, str(row["id"]))
    await record_context_audit(
        conn,
        program_id=str(row["id"]),
        actor_user_id=uid,
        action="PROGRAM_SUBMITTED_FOR_REVIEW",
        metadata={"approvalStatus": STATUS_PENDING_REVIEW},
    )
    return _format_program_row(row, active)


async def lookup_invite_preview(conn: Any, raw_code: str) -> Dict[str, Any]:
    code = (raw_code or "").strip().upper()
    if not _INVITE_CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="Código do programa inválido.")

    row = await conn.fetchrow(
        """
        SELECT id::text AS id,
               company_name,
               program_name,
               primary_color,
               logo_url,
               invite_code,
               invite_paused,
               seat_limit
          FROM partner_programs
         WHERE UPPER(invite_code) = $1
           AND approval_status = 'APPROVED'
        """,
        code,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Programa não encontrado ou ainda não disponível para adesão.",
        )

    active = await _count_active_participants(conn, str(row["id"]))
    seat_limit = int(row["seat_limit"] or 10)
    invite_paused = bool(row["invite_paused"])
    has_seats = active < seat_limit and not invite_paused
    return {
        "programId": str(row["id"]),
        "companyName": row["company_name"],
        "programName": row["program_name"],
        "primaryColor": row["primary_color"],
        "logoUrl": row["logo_url"],
        "inviteCode": row["invite_code"],
        "invitePaused": invite_paused,
        "hasAvailableSeats": has_seats,
    }
