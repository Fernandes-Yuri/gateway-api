"""Pausa com reativação explícita e fila persistente de comprovantes de e-mail."""
import asyncio
from datetime import datetime, timezone
from fastapi import HTTPException


async def account_contact(user_id):
    """Identidade autenticada é a fonte do destino; nunca aceita e-mail do body."""
    from firebase_admin import auth
    try:
        user = await asyncio.to_thread(auth.get_user, user_id)
        return (user.email or "").strip(), user.display_name or ""
    except Exception:
        return "", ""


async def queue_mail(conn, user_id, event_type, email, name, event_key):
    if email:
        await conn.execute(
            "INSERT INTO account_lifecycle_mail(event_key,user_id,event_type,to_email,user_name) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING", event_key, user_id, event_type, email, name)


async def deactivate_account(pool, user_id, email="", name=""):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT paused_at,full_name FROM profiles WHERE id=$1 FOR UPDATE", user_id)
            if row is None:
                raise HTTPException(404, "Perfil não encontrado.")
            if row["paused_at"] is None:
                paused = await conn.fetchval("UPDATE profiles SET paused_at=NOW(),session_revoked_at=NOW(),session_version=session_version+1 WHERE id=$1 RETURNING paused_at", user_id)
                await queue_mail(conn, user_id, "paused", email, row["full_name"] or name,
                                 f"paused:{user_id}:{paused.isoformat()}")
            await conn.execute("UPDATE device_push_tokens SET is_active=FALSE WHERE user_id=$1", user_id)
    # Remove o espelho legado para não reenviar push quando não houver token ativo.
    from core.devices import load_devices, save_devices
    devices = load_devices()
    if user_id in devices:
        devices.pop(user_id)
        save_devices(devices)
    return {"success": True, "paused": True}


async def reactivate_account(pool, user_id, auth_time):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT paused_at FROM profiles WHERE id=$1 FOR UPDATE", user_id)
            # Cadastro novo ainda não possui perfil; não cria um automaticamente.
            if row and row["paused_at"] is not None:
                if auth_time is None or auth_time <= int(row["paused_at"].timestamp()):
                    raise HTTPException(401, "Faça um novo login para reativar sua conta.")
                await conn.execute("UPDATE profiles SET paused_at=NULL WHERE id=$1", user_id)
    # Tokens antigos continuam inativos. Cada aparelho deve registrar o token após login.
    return {"success": True, "paused": False}


async def process_account_mail(pool, limit=20):
    """Aceite SMTP é auditado; falhas ficam na fila, sem prometer entrega na caixa."""
    from email_service import send_account_deactivation_email, send_account_deletion_email
    from services.firebase_account_reconciliation import account_exists, cleanup_legacy_firebase_storage
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock(710031)"):
            return
        try:
            rows = await conn.fetch("SELECT * FROM account_lifecycle_mail WHERE next_attempt_at<=NOW() ORDER BY created_at LIMIT $1", limit)
            for row in rows:
                key, uid = row["event_key"], row["user_id"]
                try:
                    if row["event_type"] == "deleted":
                        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=$1)", uid):
                            # Não envia comprovante antigo nem expurga uma conta recriada.
                            continue
                        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM account_storage_cleanup WHERE user_id=$1)", uid):
                            continue
                        if await account_exists(uid):
                            continue
                        await cleanup_legacy_firebase_storage(uid)
                        result = await send_account_deletion_email(row["to_email"], row["user_name"])
                    else:
                        result = await send_account_deactivation_email(row["to_email"], row["user_name"])
                    if result.get("success") is True:
                        async with conn.transaction():
                            if row["event_type"] == "deleted":
                                await conn.execute("UPDATE user_deletion_logs SET confirmation_sent_at=$2 WHERE user_id=$1 AND confirmation_sent_at IS NULL",
                                                   uid, datetime.now(timezone.utc))
                            # Remove destino/nome após o envio, sem duplicar PII na auditoria.
                            await conn.execute("DELETE FROM account_lifecycle_mail WHERE event_key=$1", key)
                        continue
                except Exception:
                    pass
                await conn.execute("UPDATE account_lifecycle_mail SET attempts=attempts+1,next_attempt_at=NOW()+INTERVAL '5 minutes' WHERE event_key=$1", key)
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock(710031)")
