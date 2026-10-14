"""Identidade Google exclusiva do worker de exclusão, sem chave compartilhada."""
import asyncio

from fastapi import HTTPException, Request
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from firebase_admin import auth


DELETION_AUDIENCE = "https://api.bragasaude.online/api/admin/firebase-auth-deletion"
DELETION_SERVICE_ACCOUNT = "account-deletion@braga-saude.iam.gserviceaccount.com"


async def require_deletion_worker(request: Request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise HTTPException(401, "Identidade de serviço necessária.")
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token, header[7:].strip(), GoogleRequest(), DELETION_AUDIENCE
        )
    except TransportError:
        raise HTTPException(503, "Verificação de identidade temporariamente indisponível.")
    except Exception:
        raise HTTPException(401, "Identidade de serviço inválida.")
    if claims.get("email") != DELETION_SERVICE_ACCOUNT or claims.get("email_verified") is not True:
        raise HTTPException(403, "Serviço não autorizado para exclusão.")
    return DELETION_SERVICE_ACCOUNT


async def require_deleted_firebase_user(user_id: str):
    """Não expurga conta ainda ativa ou recriada enquanto um evento aguardava retry."""
    try:
        await asyncio.to_thread(auth.get_user, user_id)
    except auth.UserNotFoundError:
        return
    except Exception:
        raise HTTPException(503, "Não foi possível confirmar a exclusão no Firebase.")
    raise HTTPException(409, "Conta ainda existente no Firebase; expurgo não autorizado.")
