"""Portao de conta vinculada no WhatsApp.

Regra do usuario: quem nao tem vinculo confirmado so recebe mensagem
institucional (em tom de convite para o app), MANTENDO a triagem de emergencia
ativa, porque uma pessoa com dor no peito nao pode ser abandonada por falta de
cadastro. Ambiguidade (mais de uma conta no mesmo numero) interrompe e pede
regularizacao; indisponibilidade do banco nunca vira "sem conta".

A identidade e resolvida por services.whatsapp_identity (passo 1.2): so vale o
vinculo confirmado por TOTP. Fallback por phone / emergency_contact_phone foi
removido do canal de saude.

Ordem do webhook (importante!):
  1. prompt-injection guard
  2. tentativa de vinculo TOTP          <- anotima permitida; e assim que se conquista a conta
  3. emergencia (deterministica)        <- anotima permitida; salva vida
  4. portao de conta vinculada         <- sem vinculo/ambiguo: institucional, para tudo
  5. fast-lane / router / LLM          <- so quem tem vinculo confirmado
"""
from typing import Any, Awaitable, Callable, Optional

from orb_router import detect_all
from services.whatsapp_identity import (
    STATUS_OK,
    is_confirmed,
    reply_for_status,
    resolve_linked_account,
)

# Resposta de emergencia deterministica (sem LLM) para qualquer remetente.
_MSG_EMERGENCIA = (
    "Isso parece urgente. Acione o SAMU 192 ou vá a UPA/pronto-socorro mais "
    "próximo agora."
)


async def sender_has_linked_account(db_pool: Any, sender_phone: str) -> Optional[str]:
    """
    Retorna o user_id vinculado ao numero, ou None. Delega para a resolucao
    de identidade: so vale o vinculo confirmado (TOTP), nunca phone /
    emergency_contact_phone.
    """
    if db_pool is None:
        return None
    async with db_pool.acquire() as conn:
        identity = await resolve_linked_account(conn, sender_phone)
    return identity["user_id"] if is_confirmed(identity) else None


def is_emergency(text: str) -> bool:
    """Triagem deterministica antes do portao. Reusa detect_all do router."""
    try:
        return detect_all(text or "").get("kind") == "emergency"
    except Exception:
        return False


async def gate_unregistered(
    user_text: str,
    identity: Optional[dict],
    send_text: Callable[[str, str], Awaitable[Any]],
    sender_phone: str,
) -> bool:
    """
    True se consumiu a mensagem (respondeu e o webhook para por aqui).

    - emergencia -> responde SAMU e para (salva vida, mesmo sem conta)
    - vinculo confirmado -> retorna False (segue fluxo normal)
    - sem vinculo / ambiguo / indisponivel -> mensagem do estado e para
    """
    status = (identity or {}).get("status")
    if status == STATUS_OK:
        return False
    if is_emergency(user_text):
        await send_text(sender_phone, _MSG_EMERGENCIA)
        return True
    # Mensagem do estado do vinculo (sem vinculo / ambiguo / indisponivel),
    # sempre da fonte unica do servico de identidade.
    await send_text(sender_phone, reply_for_status(status))
    return True
