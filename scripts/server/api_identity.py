"""Firebase identity for profile and family operations; roles never authenticate."""
import asyncio
from fastapi import HTTPException, Request


async def firebase_uid(request: Request):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        raise HTTPException(401, 'Entre na sua conta para continuar.')
    try:
        import firebase_admin
        from firebase_admin import auth
        if not firebase_admin._apps:
            raise HTTPException(503, 'Autenticação do servidor não configurada.')
        claims = await asyncio.to_thread(auth.verify_id_token, header[7:].strip())
        return claims['uid']
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, 'Sessão inválida ou expirada. Entre novamente.')


def require_owner(uid, user_id):
    if uid != user_id:
        raise HTTPException(403, 'Esta operação pertence a outra conta.')


async def require_family_access(conn, uid, patient_id):
    if uid == patient_id:
        return
    allowed = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM family_bindings WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE')",
        patient_id, uid)
    if not allowed:
        raise HTTPException(403, 'O acompanhamento deste familiar não está autorizado.')
