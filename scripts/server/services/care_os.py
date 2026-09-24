import hashlib
import secrets
from datetime import datetime, timezone
from typing import Iterable

from fastapi import HTTPException


ROLE_PERMISSIONS = {
    "ADMIN_CHILD": {"care:read", "care:write", "medication:write", "medication:take", "medical:share", "emergency:share"},
    "CAREGIVER_VIEWER": {"care:read"},
    "PROFESSIONAL_NURSE": {"care:read", "care:write", "medication:write", "medication:take"},
}


def new_public_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def as_iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


async def require_patient_permission(conn, uid: str, patient_id: str, permission: str):
    if uid == patient_id:
        return {"role": "PATIENT", "permissions": ["*"]}
    row = await conn.fetchrow(
        """SELECT caregiver_role, permissions FROM family_bindings
           WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE'""",
        patient_id, uid,
    )
    if not row:
        raise HTTPException(403, "O acompanhamento deste familiar não está autorizado.")
    role = row.get("caregiver_role") or "CAREGIVER_VIEWER"
    explicit = set(row.get("permissions") or [])
    if permission not in ROLE_PERMISSIONS.get(role, set()) | explicit:
        raise HTTPException(403, "Seu vínculo não permite esta operação.")
    return {"role": role, "permissions": sorted(explicit)}


def row_dict(row, *, json_fields: Iterable[str] = ()):
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
    for key in json_fields:
        result[key] = result.get(key) or {}
    return result
