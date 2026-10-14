"""DT-001: expurgo após duas ausências confirmadas de uma conta antes observada ativa."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import auth, storage

from services.account_deletion import delete_account

logger = logging.getLogger(__name__)
PROJECT_ID = "braga-saude"
LEGACY_BUCKET = "braga-saude.firebasestorage.app"
RECONCILIATION_LOCK = 560013
MISSING_GRACE = timedelta(minutes=5)


def require_project():
    if firebase_admin.get_app().project_id != PROJECT_ID:
        raise RuntimeError("Firebase project mismatch; reconciliation disabled")


async def account_exists(user_id):
    require_project()
    try:
        await asyncio.to_thread(auth.get_user, user_id)
    except auth.UserNotFoundError:
        return False
    # Timeout, permissão e qualquer outra falha propagam; nunca equivalem a ausência.
    return True


async def cleanup_legacy_firebase_storage(user_id):
    """Executa dentro da fila persistente já usada pelo expurgo local."""
    require_project()
    bucket = storage.bucket(LEGACY_BUCKET)
    if not await asyncio.to_thread(bucket.exists):
        return
    for root in ("exams", "uploads"):
        def delete_prefix():
            for blob in bucket.list_blobs(prefix=f"{root}/{user_id}/"):
                blob.delete()
        await asyncio.to_thread(delete_prefix)


async def observe_active(conn, user_id):
    await conn.execute(
        "UPDATE account_auth_observations SET observed_active_at=NOW(),missing_since=NULL,"
        "last_checked_at=NOW(),last_result='active' WHERE user_id=$1", user_id
    )


async def reconcile_accounts(pool, *, exists=account_exists, delete=delete_account, limit=100):
    stats = {key: 0 for key in ("checked", "active", "missing", "historical_unconfirmed", "unavailable", "purged", "cleanup_pending")}
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", RECONCILIATION_LOCK):
            return {**stats, "busy": True}
        try:
            await conn.execute(
                "INSERT INTO account_auth_observations(user_id) SELECT id FROM profiles ON CONFLICT DO NOTHING"
            )
            rows = await conn.fetch(
                "SELECT user_id,observed_active_at,missing_since FROM account_auth_observations "
                "ORDER BY last_checked_at,user_id LIMIT $1", limit
            )
            for row in rows:
                user_id = row["user_id"]
                stats["checked"] += 1
                try:
                    if await exists(user_id):
                        await observe_active(conn, user_id)
                        stats["active"] += 1
                        continue
                    if row["observed_active_at"] is None:
                        await conn.execute(
                            "UPDATE account_auth_observations SET last_checked_at=NOW(),"
                            "last_result='historical_unconfirmed' WHERE user_id=$1", user_id
                        )
                        stats["historical_unconfirmed"] += 1
                        continue
                    stats["missing"] += 1
                    missing_since = row["missing_since"]
                    await conn.execute(
                        "UPDATE account_auth_observations SET missing_since=COALESCE(missing_since,NOW()),"
                        "last_checked_at=NOW(),last_result='missing' WHERE user_id=$1", user_id
                    )
                    if missing_since is None or datetime.now(timezone.utc) - missing_since < MISSING_GRACE:
                        continue
                    # Última verificação antes da operação irreversível: conta recriada fica intacta.
                    if await exists(user_id):
                        await observe_active(conn, user_id)
                        stats["active"] += 1
                        continue
                    result = await delete(pool, user_id, actor="Firebase Auth reconciliation", firebase_cleanup=True)
                    stats["purged" if result.get("success") is True else "cleanup_pending"] += 1
                except Exception:
                    # Uma falha interrompe a sequência de ausências confirmadas.
                    await conn.execute(
                        "UPDATE account_auth_observations SET missing_since=NULL,last_checked_at=NOW(),"
                        "last_result='unavailable' WHERE user_id=$1", user_id
                    )
                    stats["unavailable"] += 1
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", RECONCILIATION_LOCK)
    # Apenas contagens, sem UID, dados pessoais, DSN ou respostas do Firebase.
    logger.info("Firebase account reconciliation: %s", stats)
    return stats
