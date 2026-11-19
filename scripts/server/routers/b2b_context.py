"""Router de identidade, troca de contexto (B2B-04) e onboarding empresarial (B2B-07).

Rotas aditivas que preservam 100% os contratos de autenticacao e sincronizacao
do app em teste aberto (v1.9.6 build 43):
- GET  /api/b2b/contexts
- POST /api/b2b/contexts/select
- GET  /api/b2b/program/me
- PUT  /api/b2b/program/me
- POST /api/b2b/program/me/submit
- GET  /api/b2b/invite/{code}
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api_identity import firebase_uid
from core.b2b_context import (
    MODE_PERSONAL,
    VALID_CONTEXT_MODES,
    list_user_contexts,
    personal_context,
    record_context_audit,
    resolve_program_context,
)
from core.b2b_onboarding import (
    DEFAULT_PRIMARY_COLOR,
    get_my_partner_program,
    lookup_invite_preview,
    submit_my_partner_program,
    upsert_my_partner_program,
)

router = APIRouter()


class _AppState:
    db_pool = None


_state = _AppState()


def configure_state(*, db_pool=None):
    _state.db_pool = db_pool


class SelectContextRequest(BaseModel):
    mode: str = MODE_PERSONAL
    programId: Optional[str] = None


class UpsertPartnerProgramRequest(BaseModel):
    companyName: str
    programName: str
    cnpj: Optional[str] = None
    primaryColor: Optional[str] = DEFAULT_PRIMARY_COLOR
    logoUrl: Optional[str] = None


@router.get("/api/b2b/contexts")
async def get_available_contexts(request: Request):
    """Retorna os contextos autorizados para o usuario autenticado via JWT RS256."""
    uid = await firebase_uid(request)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        contexts = await list_user_contexts(conn, uid)

    return {
        "defaultMode": MODE_PERSONAL,
        "contexts": contexts,
    }


@router.post("/api/b2b/contexts/select")
async def select_active_context(request: Request, req: SelectContextRequest):
    """Valida e confirma a troca de contexto entre Uso Pessoal e Programa Parceiro."""
    uid = await firebase_uid(request)
    mode = (req.mode or "").strip().upper()
    if mode not in VALID_CONTEXT_MODES:
        raise HTTPException(status_code=400, detail="Modo de contexto inválido.")

    if mode == MODE_PERSONAL:
        return {"activeContext": personal_context(uid)}

    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        resolved = await resolve_program_context(
            conn,
            uid,
            req.programId or "",
            requested_mode=mode,
        )
        await record_context_audit(
            conn,
            program_id=resolved["programId"],
            actor_user_id=uid,
            action="CONTEXT_SELECTED",
            metadata={"mode": resolved["mode"]},
        )

    return {"activeContext": resolved}


@router.get("/api/b2b/program/me")
async def get_my_program(request: Request):
    """Consulta o cadastro e status do programa empresarial do usuario autenticado."""
    uid = await firebase_uid(request)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        program = await get_my_partner_program(conn, uid)

    return {"program": program}


@router.put("/api/b2b/program/me")
async def save_my_program(request: Request, req: UpsertPartnerProgramRequest):
    """Cria ou atualiza a preparacao e identidade visual do programa do parceiro."""
    uid = await firebase_uid(request)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        program = await upsert_my_partner_program(
            conn,
            uid,
            company_name=req.companyName,
            program_name=req.programName,
            cnpj=req.cnpj,
            primary_color=req.primaryColor,
            logo_url=req.logoUrl,
        )

    return {"program": program}


@router.post("/api/b2b/program/me/submit")
async def submit_my_program_for_review(request: Request):
    """Envia o programa em preparacao para analise manual da equipe Braga."""
    uid = await firebase_uid(request)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        program = await submit_my_partner_program(conn, uid)

    return {"program": program}


@router.get("/api/b2b/invite/{code}")
async def get_invite_preview(code: str):
    """Pre-visualizacao publica da marca do parceiro antes do login ou vinculo."""
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível.")

    async with db_pool.acquire() as conn:
        invite = await lookup_invite_preview(conn, code)

    return {"invite": invite}
