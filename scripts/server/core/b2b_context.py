"""Resolucao autoritativa de contexto B2C / B2B (B2B-04).

Principios de seguranca:
- Identidade vem exclusivamente do JWT RS256 validado (`api_identity.firebase_uid`).
- Headers de identidade ou papel enviados pelo cliente (`x-user-id`, `x-role`,
  `x-internal-*`, `x-context-mode`, `x-program-role`) sao sanitizados na borda
  Rust (`braga-edge`) e jamais confiados pelo backend Python.
- Ausencia de contexto B2B mantem o modo `PERSONAL` padrao, preservando 100%
  dos fluxos do app publicado em teste aberto (v1.9.6 build 43).
- Qualquer acesso a um `program_id` exige comprobacao no PostgreSQL
  (`partner_programs` para dono ou `program_enrollments` ativo em programa
  aprovado para participante).
"""
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from security_telemetry import mark_signal

MODE_PERSONAL = "PERSONAL"
MODE_PARTNER_OWNER = "PARTNER_OWNER"
MODE_PARTNER_PARTICIPANT = "PARTNER_PARTICIPANT"

VALID_CONTEXT_MODES = {
    MODE_PERSONAL,
    MODE_PARTNER_OWNER,
    MODE_PARTNER_PARTICIPANT,
}

PROGRAM_ID_HEADER = "x-braga-program-id"
CONTEXT_MODE_HEADER = "x-braga-context-mode"


def personal_context(uid: str) -> Dict[str, Any]:
    """Contexto pessoal padrao (B2C)."""
    return {
        "mode": MODE_PERSONAL,
        "userId": uid,
        "programId": None,
        "programName": None,
        "companyName": None,
        "primaryColor": None,
        "logoUrl": None,
        "approvalStatus": None,
        "sharedCategories": [],
    }


def _normalize_program_uuid(raw_program_id: Optional[str]) -> str:
    if not raw_program_id or not str(raw_program_id).strip():
        raise HTTPException(status_code=400, detail="Identificador do programa ausente.")
    try:
        return str(uuid.UUID(str(raw_program_id).strip()))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Identificador do programa inválido.")


def _normalize_categories(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if isinstance(item, str)]
        except Exception:
            return []
    return []


async def list_user_contexts(conn: Any, uid: str) -> List[Dict[str, Any]]:
    """Lista os contextos autorizados para `uid` (sempre iniciando por PERSONAL)."""
    contexts: List[Dict[str, Any]] = [personal_context(uid)]

    owner_rows = await conn.fetch(
        """
        SELECT id::text AS id,
               program_name,
               company_name,
               primary_color,
               logo_url,
               approval_status
          FROM partner_programs
         WHERE owner_user_id = $1
         ORDER BY created_at ASC
        """,
        uid,
    )
    for row in owner_rows:
        contexts.append(
            {
                "mode": MODE_PARTNER_OWNER,
                "userId": uid,
                "programId": str(row["id"]),
                "programName": row["program_name"],
                "companyName": row["company_name"],
                "primaryColor": row["primary_color"],
                "logoUrl": row["logo_url"],
                "approvalStatus": row["approval_status"],
                "sharedCategories": [],
            }
        )

    participant_rows = await conn.fetch(
        """
        SELECT p.id::text AS id,
               p.program_name,
               p.company_name,
               p.primary_color,
               p.logo_url,
               p.approval_status,
               e.shared_categories
          FROM program_enrollments e
          JOIN partner_programs p ON p.id = e.program_id
         WHERE e.patient_user_id = $1
           AND e.status = 'ACTIVE'
           AND p.approval_status = 'APPROVED'
         ORDER BY e.consented_at ASC
        """,
        uid,
    )
    for row in participant_rows:
        contexts.append(
            {
                "mode": MODE_PARTNER_PARTICIPANT,
                "userId": uid,
                "programId": str(row["id"]),
                "programName": row["program_name"],
                "companyName": row["company_name"],
                "primaryColor": row["primary_color"],
                "logoUrl": row["logo_url"],
                "approvalStatus": row["approval_status"],
                "sharedCategories": _normalize_categories(row["shared_categories"]),
            }
        )

    return contexts


async def resolve_program_context(
    conn: Any,
    uid: str,
    program_id: str,
    requested_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Valida no PostgreSQL se `uid` possui vinculo ou autoria sobre `program_id`."""
    normalized_id = _normalize_program_uuid(program_id)
    mode = (requested_mode or "").strip().upper() or None
    if mode is not None and mode not in {MODE_PARTNER_OWNER, MODE_PARTNER_PARTICIPANT}:
        raise HTTPException(status_code=400, detail="Modo de contexto B2B inválido.")

    if mode in (None, MODE_PARTNER_OWNER):
        owner_row = await conn.fetchrow(
            """
            SELECT id::text AS id,
                   program_name,
                   company_name,
                   primary_color,
                   logo_url,
                   approval_status
              FROM partner_programs
             WHERE id = $1::uuid
               AND owner_user_id = $2
            """,
            normalized_id,
            uid,
        )
        if owner_row:
            return {
                "mode": MODE_PARTNER_OWNER,
                "userId": uid,
                "programId": str(owner_row["id"]),
                "programName": owner_row["program_name"],
                "companyName": owner_row["company_name"],
                "primaryColor": owner_row["primary_color"],
                "logoUrl": owner_row["logo_url"],
                "approvalStatus": owner_row["approval_status"],
                "sharedCategories": [],
            }

    if mode in (None, MODE_PARTNER_PARTICIPANT):
        participant_row = await conn.fetchrow(
            """
            SELECT p.id::text AS id,
                   p.program_name,
                   p.company_name,
                   p.primary_color,
                   p.logo_url,
                   p.approval_status,
                   e.shared_categories
              FROM program_enrollments e
              JOIN partner_programs p ON p.id = e.program_id
             WHERE e.program_id = $1::uuid
               AND e.patient_user_id = $2
               AND e.status = 'ACTIVE'
               AND p.approval_status = 'APPROVED'
            """,
            normalized_id,
            uid,
        )
        if participant_row:
            return {
                "mode": MODE_PARTNER_PARTICIPANT,
                "userId": uid,
                "programId": str(participant_row["id"]),
                "programName": participant_row["program_name"],
                "companyName": participant_row["company_name"],
                "primaryColor": participant_row["primary_color"],
                "logoUrl": participant_row["logo_url"],
                "approvalStatus": participant_row["approval_status"],
                "sharedCategories": _normalize_categories(participant_row["shared_categories"]),
            }

    mark_signal("forbidden_resource")
    raise HTTPException(status_code=403, detail="Acesso não autorizado a este programa.")


async def record_context_audit(
    conn: Any,
    *,
    program_id: Optional[str],
    actor_user_id: str,
    action: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Registra evento de auditoria append-only em `program_audit_logs`."""
    await conn.execute(
        """
        INSERT INTO program_audit_logs (program_id, actor_user_id, target_user_id, action, metadata)
        VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
        """,
        program_id,
        actor_user_id,
        actor_user_id,
        action,
        json.dumps(metadata or {}),
    )


async def resolve_request_context(
    request: Request,
    db_pool: Any,
    uid: str,
) -> Dict[str, Any]:
    """Resolve o contexto da requisicao atual.

    - Se `X-Braga-Program-Id` nao for informado (ou modo for PERSONAL), retorna
      imediatamente o contexto `PERSONAL` sem exigir consulta ao banco.
    - Se `X-Braga-Program-Id` for informado, exige verificacao no banco contra
      o `uid` do JWT RS256.
    """
    raw_program_id = (request.headers.get(PROGRAM_ID_HEADER) or "").strip()
    raw_mode = (request.headers.get(CONTEXT_MODE_HEADER) or "").strip().upper()

    if not raw_program_id and raw_mode in ("", MODE_PERSONAL):
        return personal_context(uid)

    if raw_mode == MODE_PERSONAL and not raw_program_id:
        return personal_context(uid)

    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        return await resolve_program_context(
            conn,
            uid,
            raw_program_id,
            requested_mode=raw_mode or None,
        )
