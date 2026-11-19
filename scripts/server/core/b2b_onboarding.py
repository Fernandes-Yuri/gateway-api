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
import secrets
import unicodedata
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from core.b2b_context import record_context_audit

STATUS_DRAFT = "DRAFT"
STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_CHANGES_REQUESTED = "CHANGES_REQUESTED"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"

VALID_APPROVAL_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_REVIEW,
    STATUS_CHANGES_REQUESTED,
    STATUS_APPROVED,
    STATUS_REJECTED,
}

DEFAULT_PRIMARY_COLOR = "#1A6B5C"
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


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


def generate_invite_code(program_name: str) -> str:
    """Gera codigo de convite legivel e sem caracteres ambiguos (ex.: HORI-7K9P)."""
    normalized = unicodedata.normalize("NFKD", program_name or "")
    ascii_letters = "".join(
        ch for ch in normalized.upper() if "A" <= ch <= "Z"
    )
    prefix = (ascii_letters[:4] or "BRAG").ljust(4, "X")
    suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"{prefix}-{suffix}"


async def list_partner_programs_for_admin(
    conn: Any,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Lista solicitacoes de programas parceiros para a fila administrativa (B2B-09)."""
    clean_status = (status or "").strip().upper() or None
    if clean_status is not None and clean_status not in VALID_APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="Status de aprovação inválido.")

    safe_limit = max(1, min(int(limit), 200))
    safe_offset = max(0, int(offset))

    rows = await conn.fetch(
        """
        SELECT p.id::text AS id,
               p.owner_user_id,
               p.company_name,
               p.program_name,
               p.cnpj,
               p.primary_color,
               p.logo_url,
               p.approval_status,
               p.review_note,
               p.reviewed_by,
               p.reviewed_at,
               p.invite_code,
               p.invite_paused,
               p.seat_limit,
               p.created_at,
               p.updated_at,
               COALESCE(
                   (SELECT COUNT(*)
                      FROM program_enrollments e
                     WHERE e.program_id = p.id
                       AND e.status = 'ACTIVE'),
                   0
               ) AS active_participants
          FROM partner_programs p
         WHERE ($1::text IS NULL OR p.approval_status = $1)
         ORDER BY
               CASE WHEN p.approval_status = 'PENDING_REVIEW' THEN 0 ELSE 1 END,
               p.updated_at DESC
         LIMIT $2 OFFSET $3
        """,
        clean_status,
        safe_limit,
        safe_offset,
    )

    items: List[Dict[str, Any]] = []
    for row in rows:
        item = _format_program_row(row, int(row["active_participants"] or 0))
        item["reviewedBy"] = row["reviewed_by"]
        item["reviewedAt"] = (
            row["reviewed_at"].isoformat() if row["reviewed_at"] else None
        )
        item["createdAt"] = (
            row["created_at"].isoformat() if row["created_at"] else None
        )
        item["updatedAt"] = (
            row["updated_at"].isoformat() if row["updated_at"] else None
        )
        items.append(item)
    return items


async def review_partner_program(
    conn: Any,
    *,
    program_id: str,
    action: str,
    reviewer: str,
    note: Optional[str] = None,
    seat_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Executa a maquina de estados de aprovacao manual assistida (B2B-09).

    Somente programas em PENDING_REVIEW podem receber decisao da equipe Braga.
    """
    try:
        normalized_id = str(uuid.UUID(str(program_id).strip()))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Identificador do programa inválido.")

    clean_action = (action or "").strip().lower()
    if clean_action not in {"approve", "request_changes", "reject"}:
        raise HTTPException(
            status_code=400,
            detail="Ação inválida. Use approve, request_changes ou reject.",
        )

    clean_reviewer = (reviewer or "").strip()[:128] or "equipe-braga"
    clean_note = (note or "").strip()
    if len(clean_note) > 600:
        raise HTTPException(
            status_code=400,
            detail="A mensagem para o parceiro deve ter no máximo 600 caracteres.",
        )

    if clean_action in {"request_changes", "reject"} and len(clean_note) < 5:
        raise HTTPException(
            status_code=400,
            detail="Informe uma justificativa (mínimo 5 caracteres) ao solicitar ajustes ou recusar.",
        )

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
         WHERE id = $1::uuid
           FOR UPDATE
        """,
        normalized_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Programa parceiro não encontrado.")

    if existing["approval_status"] != STATUS_PENDING_REVIEW:
        raise HTTPException(
            status_code=409,
            detail="Apenas solicitações em análise (PENDING_REVIEW) podem receber decisão.",
        )

    next_invite_code = existing["invite_code"]
    next_seat_limit = int(existing["seat_limit"] or 10)

    if clean_action == "approve":
        next_status = STATUS_APPROVED
        audit_action = "PROGRAM_APPROVED"
        if seat_limit is not None:
            if seat_limit < 1 or seat_limit > 10000:
                raise HTTPException(
                    status_code=400,
                    detail="Limite de vagas deve estar entre 1 e 10000.",
                )
            next_seat_limit = int(seat_limit)
        if not next_invite_code:
            for _ in range(5):
                candidate = generate_invite_code(existing["program_name"])
                collision = await conn.fetchval(
                    "SELECT 1 FROM partner_programs WHERE UPPER(invite_code) = $1",
                    candidate,
                )
                if not collision:
                    next_invite_code = candidate
                    break
            if not next_invite_code:
                raise HTTPException(
                    status_code=500,
                    detail="Não foi possível gerar um código de convite único.",
                )
    elif clean_action == "request_changes":
        next_status = STATUS_CHANGES_REQUESTED
        audit_action = "PROGRAM_CHANGES_REQUESTED"
    else:
        next_status = STATUS_REJECTED
        audit_action = "PROGRAM_REJECTED"

    updated = await conn.fetchrow(
        """
        UPDATE partner_programs
           SET approval_status = $2,
               review_note = $3,
               reviewed_by = $4,
               reviewed_at = NOW(),
               invite_code = $5,
               seat_limit = $6,
               updated_at = NOW()
         WHERE id = $1::uuid
         RETURNING id::text AS id,
                   owner_user_id,
                   company_name,
                   program_name,
                   cnpj,
                   primary_color,
                   logo_url,
                   approval_status,
                   review_note,
                   reviewed_by,
                   reviewed_at,
                   invite_code,
                   invite_paused,
                   seat_limit
        """,
        normalized_id,
        next_status,
        clean_note or None,
        clean_reviewer,
        next_invite_code,
        next_seat_limit,
    )

    active = await _count_active_participants(conn, normalized_id)
    await record_context_audit(
        conn,
        program_id=normalized_id,
        actor_user_id=clean_reviewer,
        action=audit_action,
        metadata={
            "previousStatus": STATUS_PENDING_REVIEW,
            "approvalStatus": next_status,
            "seatLimit": next_seat_limit,
        },
    )

    result = _format_program_row(updated, active)
    result["reviewedBy"] = updated["reviewed_by"]
    result["reviewedAt"] = (
        updated["reviewed_at"].isoformat() if updated["reviewed_at"] else None
    )
    return result
