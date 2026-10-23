"""Restrições autoritativas, sem cache que prolongue restrições expiradas."""
from fastapi import HTTPException

def _pool_provider():
    return None


def configure_pool(provider):
    global _pool_provider
    _pool_provider = provider


async def require_not_restricted(uid, request, *, auth_time=None):
    path = request.url.path
    method = getattr(request, 'method', 'POST')
    if path.startswith('/api/admin/') or (path == '/api/app/latest' and method == 'GET'):
        return
    pool = _pool_provider()
    if pool is None:
        raise HTTPException(503, 'Banco de dados indisponível.')
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT level,reason FROM user_restrictions WHERE user_id=$1 AND (expires_at IS NULL OR expires_at > NOW())", uid)
    if row:
        level = row['level']
        denied = (level == 'suspended' or
              (level == 'read-only' and method not in ('GET', 'HEAD', 'OPTIONS')) or
              (level == 'no-sync' and method == 'POST' and
               path.startswith('/api/sync/')))
        if denied:
            from security_telemetry import mark_signal
            mark_signal('restricted_access')
            raise HTTPException(401 if level == 'suspended' else 403, row['reason'])
    async with pool.acquire() as conn:
        paused = await conn.fetchval('SELECT paused_at FROM profiles WHERE id=$1', uid)
    if paused is not None:
        if path != f'/api/profile/{uid}/reactivate' or method != 'POST':
            raise HTTPException(403, 'Conta pausada. Faça um novo login para retornar.')
        if auth_time is None or auth_time <= int(paused.timestamp()):
            raise HTTPException(401, 'Faça um novo login para reativar sua conta.')
    if auth_time is not None:
        async with pool.acquire() as conn:
            revoked_at = await conn.fetchval('SELECT session_revoked_at FROM profiles WHERE id=$1', uid)
        if revoked_at is not None and auth_time <= int(revoked_at.timestamp()):
            raise HTTPException(401, 'Sessão revogada. Entre novamente.')
