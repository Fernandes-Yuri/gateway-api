"""Firebase identity for profile and family operations; roles never authenticate."""
import asyncio
from fastapi import HTTPException, Request
from security_telemetry import mark_signal


async def firebase_uid(request: Request):
    cached_uid = getattr(request.state, 'firebase_verified_uid', None)
    if cached_uid:
        return cached_uid
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        mark_signal('auth_required')
        raise HTTPException(401, 'Entre na sua conta para continuar.')
    try:
        import firebase_admin
        from firebase_admin import auth
        if not firebase_admin._apps:
            raise HTTPException(503, 'Autenticação do servidor não configurada.')
        claims = await asyncio.to_thread(auth.verify_id_token, header[7:].strip())
        uid, auth_time = claims['uid'], claims['auth_time']
    except HTTPException:
        raise
    except Exception:
        mark_signal('invalid_auth')
        raise HTTPException(401, 'Sessão inválida ou expirada. Entre novamente.')
    request.state.security_verified_uid = uid
    from user_moderation import require_not_restricted
    try:
        await require_not_restricted(uid, request, auth_time=auth_time)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Verificação de acesso indisponível.')
    # WebSocket revalida a identidade em cada mensagem; não mantém cache.
    if request.scope['type'] == 'http':
        request.state.firebase_verified_uid = uid
    return uid


def require_owner(uid, user_id):
    if uid != user_id:
        mark_signal('uid_mismatch')
        raise HTTPException(403, 'Esta operação pertence a outra conta.')


async def require_family_access(conn, uid, patient_id):
    if uid == patient_id:
        return
    allowed = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM family_bindings WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE')",
        patient_id, uid)
    if not allowed:
        mark_signal('forbidden_resource')
        raise HTTPException(403, 'O acompanhamento deste familiar não está autorizado.')
