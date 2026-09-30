"""Proteções persistentes do portal; nenhuma credencial é registrada em logs."""
import base64
import hashlib
import hmac
import os
import struct
import time

from fastapi import HTTPException


async def auth_limit(pool, scope: str, subject: str, limit: int, seconds: int):
    key = hashlib.sha256(f"{scope}:{subject}".encode()).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admin_auth_limits WHERE expires_at < NOW()")
        count = await conn.fetchval(
            """INSERT INTO admin_auth_limits (key, attempts, expires_at)
               VALUES ($1, 1, NOW() + $2 * interval '1 second')
               ON CONFLICT (key) DO UPDATE SET attempts = admin_auth_limits.attempts + 1
               RETURNING attempts""", key, seconds)
    if count > limit:
        raise HTTPException(429, "Muitas tentativas. Aguarde antes de tentar novamente.")


def master_step(code: str) -> tuple[int, str]:
    secret = os.getenv("ADMIN_TOTP_SECRET", "").strip().replace(" ", "").upper().rstrip("=")
    if not secret:
        raise HTTPException(503, "Autorização mestre indisponível.")
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    except Exception:
        raise HTTPException(503, "Autorização mestre indisponível.")
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise HTTPException(400, "Código de autorização inválido.")
    now = int(time.time()) // 30
    for counter in (now - 1, now, now + 1):
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 15
        number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
        if hmac.compare_digest(f"{number % 1000000:06d}", code):
            return counter, hashlib.sha256(key).hexdigest()
    raise HTTPException(401, "Código de autorização inválido ou expirado.")


async def consume_master(conn, step: int, fingerprint: str):
    await conn.execute("DELETE FROM admin_master_uses WHERE used_at < NOW() - interval '5 minutes'")
    used = await conn.fetchval(
        """INSERT INTO admin_master_uses (fingerprint, step) VALUES ($1, $2)
           ON CONFLICT DO NOTHING RETURNING step""", fingerprint, step)
    if used is None:
        raise HTTPException(401, "Código já utilizado. Aguarde o próximo código do autenticador.")
