"""Router de identidade e troca de contexto B2C / B2B (B2B-04).

Rotas aditivas que preservam 100% os contratos de autenticacao e sincronizacao
do app em teste aberto (v1.9.6 build 43):
- GET  /api/b2b/contexts
- POST /api/b2b/contexts/select
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

router = APIRouter()


class _AppState:
    db_pool = None


_state = _AppState()


def configure_state(*, db_pool=None):
    _state.db_pool = db_pool


class SelectContextRequest(BaseModel):
    mode: str = MODE_PERSONAL
    programId: Optional[str] = None


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
