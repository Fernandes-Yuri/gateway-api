"""Exclusão transacional de conta e fila persistente de expurgo de arquivos."""
import json
import os
import re
import shutil

from fastapi import HTTPException
from config import STORAGE_DIR


def _extract_b2b_r2_keys_for_program(program_row) -> list[str]:
    keys: list[str] = []
    program_id = str(program_row.get("id") or "").strip()
    logo_url = str(program_row.get("logo_url") or "").strip()
    if program_id and logo_url:
        marker = f"/api/files/b2b-logos/{program_id}/"
        if marker in logo_url:
            file_part = logo_url.split(marker, 1)[1].split("?", 1)[0].strip()
            if file_part and "/" not in file_part and ".." not in file_part:
                keys.append(f"b2b-logos/{program_id}/{file_part}")
    proofs_raw = program_row.get("proof_documents")
    if isinstance(proofs_raw, str):
        try:
            proofs_raw = json.loads(proofs_raw)
        except Exception:
            proofs_raw = []
    if isinstance(proofs_raw, list):
        for entry in proofs_raw:
            if isinstance(entry, dict):
                skey = str(entry.get("storageKey") or "").strip()
                if skey.startswith(f"b2b-kyb/{program_id}/") and ".." not in skey:
                    keys.append(skey)
    return keys


def _delete_r2_objects_sync(keys: list[str]) -> None:
    if not keys:
        return
    bucket = (os.getenv("B2B_STORAGE_BUCKET") or os.getenv("CF_R2_BUCKET_NAME") or "").strip()
    endpoint = (os.getenv("B2B_STORAGE_ENDPOINT") or os.getenv("CF_R2_ENDPOINT_URL") or "").strip()
    access_key = (os.getenv("B2B_STORAGE_ACCESS_KEY_ID") or os.getenv("CF_R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("B2B_STORAGE_SECRET_ACCESS_KEY") or os.getenv("CF_R2_SECRET_ACCESS_KEY") or "").strip()
    if not (bucket and endpoint and access_key and secret_key):
        return
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=(os.getenv("B2B_STORAGE_REGION") or "auto").strip() or "auto",
    )
    for key in keys:
        if key.startswith(("b2b-logos/", "b2b-kyb/")) and ".." not in key:
            client.delete_object(Bucket=bucket, Key=key)


async def cleanup_account_files(pool, user_id: str) -> bool:
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT prescription_filenames, firebase_cleanup_required, "
            "COALESCE(to_jsonb(a)->'b2b_r2_keys', '[]'::jsonb) AS b2b_r2_keys "
            "FROM account_storage_cleanup a WHERE user_id=$1",
            user_id,
        )
        if not job:
            return True
        names = job["prescription_filenames"]
        if isinstance(names, str):
            names = json.loads(names)
        b2b_keys = job.get("b2b_r2_keys") if hasattr(job, "get") else job["b2b_r2_keys"]
        if isinstance(b2b_keys, str):
            b2b_keys = json.loads(b2b_keys)
        try:
            if job.get("firebase_cleanup_required"):
                from services.firebase_account_reconciliation import account_exists
                if await account_exists(user_id):
                    # Um retry antigo não pode apagar arquivos de uma conta recriada.
                    return False
            for root in (
                os.path.join(STORAGE_DIR, "avatars"),
                os.path.join(STORAGE_DIR, "medications"),
                os.path.join(STORAGE_DIR, "b2b_logos"),
            ):
                path = os.path.realpath(os.path.join(root, user_id))
                if os.path.commonpath([os.path.realpath(root), path]) != os.path.realpath(root):
                    raise ValueError("Caminho fora do armazenamento de contas")
                if os.path.isdir(path):
                    shutil.rmtree(path)
            for name in names:
                if name != os.path.basename(name):
                    raise ValueError("Nome de anexo inválido")
                path = os.path.join(STORAGE_DIR, "prescriptions", name)
                if os.path.isfile(path):
                    os.remove(path)
            if isinstance(b2b_keys, list) and b2b_keys:
                import asyncio
                await asyncio.to_thread(_delete_r2_objects_sync, [str(k) for k in b2b_keys])
            if job.get("firebase_cleanup_required"):
                from services.firebase_account_reconciliation import cleanup_legacy_firebase_storage
                await cleanup_legacy_firebase_storage(user_id)
        except Exception:
            # Não confirma expurgo incompleto; a fila permite uma nova tentativa.
            return False
        await conn.execute("DELETE FROM account_storage_cleanup WHERE user_id=$1", user_id)
    return True


async def delete_account(pool, user_id: str, *, actor: str, ip: str = "", firebase_cleanup: bool = False, receipt_email: str = "", receipt_name: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user_id):
        raise HTTPException(422, "Identificador de conta inválido.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            profile = await conn.fetchrow("SELECT id FROM profiles WHERE id=$1 FOR UPDATE", user_id)
            # Sem FK para profiles: também remove resíduos em uma repetição da exclusão.
            await conn.execute("DELETE FROM device_push_tokens WHERE user_id=$1", user_id)
            # Expurgo explícito também cobre bases legadas sem todas as FKs.
            await conn.execute(
                "DELETE FROM post_reactions WHERE user_id=$1 OR post_id IN "
                "(SELECT id FROM social_posts WHERE user_id=$1)", user_id)
            await conn.execute("DELETE FROM social_posts WHERE user_id=$1", user_id)
            if profile:
                from services.account_lifecycle import queue_mail
                await queue_mail(conn, user_id, "deleted", receipt_email, receipt_name, f"deleted:{user_id}")
                files = await conn.fetch("SELECT filename FROM prescription_files WHERE patient_id=$1", user_id)
                b2b_meta = await conn.fetch(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_attribute
                         WHERE attrelid = 'account_storage_cleanup'::regclass
                           AND attname = 'b2b_r2_keys'
                           AND NOT attisdropped
                    ) AS has_cleanup_col
                     WHERE to_regclass('partner_programs') IS NOT NULL
                    """
                )
                b2b_r2_keys: list[str] = []
                if b2b_meta:
                    b2b_rows = await conn.fetch(
                        "SELECT id::text AS id, logo_url, "
                        "COALESCE(to_jsonb(p)->'proof_documents', '[]'::jsonb) AS proof_documents "
                        "FROM partner_programs p WHERE owner_user_id=$1",
                        user_id,
                    )
                    for prow in b2b_rows or []:
                        b2b_r2_keys.extend(_extract_b2b_r2_keys_for_program(dict(prow)))
                if b2b_meta and b2b_meta[0].get("has_cleanup_col"):
                    await conn.execute(
                        "INSERT INTO account_storage_cleanup(user_id,prescription_filenames,firebase_cleanup_required,b2b_r2_keys) "
                        "VALUES ($1,$2::jsonb,$3,$4::jsonb) ON CONFLICT (user_id) DO UPDATE SET "
                        "firebase_cleanup_required=account_storage_cleanup.firebase_cleanup_required OR EXCLUDED.firebase_cleanup_required, "
                        "b2b_r2_keys=EXCLUDED.b2b_r2_keys",
                        user_id, json.dumps([r["filename"] for r in files]), firebase_cleanup, json.dumps(b2b_r2_keys))
                else:
                    await conn.execute(
                        "INSERT INTO account_storage_cleanup(user_id,prescription_filenames,firebase_cleanup_required) "
                        "VALUES ($1,$2::jsonb,$3) ON CONFLICT (user_id) DO UPDATE SET "
                        "firebase_cleanup_required=account_storage_cleanup.firebase_cleanup_required OR EXCLUDED.firebase_cleanup_required",
                        user_id, json.dumps([r["filename"] for r in files]), firebase_cleanup)
                # Feedbacks não possuem FK de perfil no esquema original.
                await conn.execute("DELETE FROM feedbacks WHERE user_id=$1", user_id)
                for table in ("medical_access_tokens", "emergency_access_tokens"):
                    await conn.execute(f"DELETE FROM {table} WHERE created_by=$1", user_id)
                await conn.execute("UPDATE care_audit_trail SET actor_name='Conta excluída',details='{}'::jsonb WHERE actor_id=$1", user_id)
                await conn.execute("DELETE FROM profiles WHERE id=$1", user_id)
                await conn.execute(
                    "INSERT INTO user_deletion_logs(user_id,ip_address,tables_affected,admin_email) VALUES ($1,$2,$3,$4)",
                    user_id, ip, "profiles,cascata,feedbacks,tokens,arquivos", actor)
    cleaned = await cleanup_account_files(pool, user_id)
    return {"success": cleaned, "deleted": True, "user_id": user_id, "storageCleanupPending": not cleaned}
