"""Exclusão transacional de conta e fila persistente de expurgo de arquivos."""
import json
import os
import re
import shutil

from fastapi import HTTPException
from config import STORAGE_DIR, STORAGE_EXAMS_DIR


async def cleanup_account_files(pool, user_id: str) -> bool:
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT prescription_filenames FROM account_storage_cleanup WHERE user_id=$1", user_id)
        if not job:
            return True
        names = job["prescription_filenames"]
        if isinstance(names, str):
            names = json.loads(names)
        try:
            for root in (os.path.join(STORAGE_DIR, "avatars"), os.path.join(STORAGE_DIR, "medications"), STORAGE_EXAMS_DIR):
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
        except (OSError, ValueError):
            # Não confirma expurgo incompleto; a fila permite uma nova tentativa.
            return False
        await conn.execute("DELETE FROM account_storage_cleanup WHERE user_id=$1", user_id)
    return True


async def delete_account(pool, user_id: str, *, actor: str, ip: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user_id):
        raise HTTPException(422, "Identificador de conta inválido.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            profile = await conn.fetchrow("SELECT id FROM profiles WHERE id=$1 FOR UPDATE", user_id)
            if profile:
                files = await conn.fetch("SELECT filename FROM prescription_files WHERE patient_id=$1", user_id)
                await conn.execute(
                    "INSERT INTO account_storage_cleanup(user_id,prescription_filenames) VALUES ($1,$2::jsonb) ON CONFLICT (user_id) DO NOTHING",
                    user_id, json.dumps([r["filename"] for r in files]))
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
