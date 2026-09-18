"""Portao de conta vinculada no WhatsApp.

Regra do usuario: quem nao tem conta vinculada so recebe mensagem institucional
(em tom de convite para o app), MANTENDO a triagem de emergencia ativa, porque
uma pessoa com dor no peito nao pode ser abandonada por falta de cadastro.

Ordem do webhook (importante!):
  1. prompt-injection guard
  2. tentativa de vinculo TOTP          <- anotima permitida; e assim que se conquista a conta
  3. emergencia (deterministica)        <- anotima permitida; salva vida
  4. portao de conta vinculada         <- sem conta: institucional, para tudo
  5. fast-lane / router / LLM          <- so quem tem conta
"""
from typing import Any, Awaitable, Callable, Optional

from orb_router import detect_all

# Mensagem institucional: acolhedora, honesta e convite claro para o app.
# Nao promete atendimento que nao existe; deixa a rede de seguranca clinica visivel.
_MSG_INSTITUCIONAL = (
    "Olá! Aqui e o Braga Saúde. 💚\n\n"
    "Nosso assistente atende apenas pessoas com conta vinculada no aplicativo - "
    "assim cuidamos do seu historico e da sua familia com toda a seguranca e "
    "privacidade.\n\n"
    "Para conversar comigo aqui no WhatsApp, e so baixar o app Braga Saúde e tocar "
    "em 'Vincular WhatsApp'. Se precisar de ajuda, escreva para "
    "contato@bragasaude.online.\n\n"
    "Se for uma emergencia (falta de ar, dor forte no peito, desmaio), chame o "
    "SAMU 192 ou va a UPA mais proxima agora."
)

# Resposta de emergencia deterministica (sem LLM) para qualquer remetente.
_MSG_EMERGENCIA = (
    "Isso parece urgente. Acione o SAMU 192 ou vá a UPA/pronto-socorro mais "
    "próximo agora."
)


async def sender_has_linked_account(db_pool: Any, sender_phone: str) -> Optional[str]:
    """
    Retorna o user_id vinculado ao numero, ou None. Busca exata pelo numero
    normalizado: substitui a busca legada por sufixo de 8 digitos (fraqueza
    conhecida do doc 37), que esta sendo descontinuada junto deste portao.
    """
    if db_pool is None:
        return None
    digits = "".join(ch for ch in sender_phone or "" if ch.isdigit())
    if not digits:
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM profiles "
            "WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1",
            digits,
        )
        return row["id"] if row else None


def is_emergency(text: str) -> bool:
    """Triagem deterministica antes do portao. Reusa detect_all do router."""
    try:
        return detect_all(text or "").get("kind") == "emergency"
    except Exception:
        return False


async def gate_unregistered(
    user_text: str,
    user_id: Optional[str],
    send_text: Callable[[str, str], Awaitable[Any]],
    sender_phone: str,
) -> bool:
    """
    True se consumiu a mensagem (respondeu e o webhook para por aqui).

    - emergencia -> responde SAMU e para (salva vida, mesmo sem conta)
    - sem conta  -> responde institucional e para
    - com conta  -> retorna False (segue fluxo normal)
    """
    if user_id is not None:
        return False
    if is_emergency(user_text):
        await send_text(sender_phone, _MSG_EMERGENCIA)
        return True
    await send_text(sender_phone, _MSG_INSTITUCIONAL)
    return True
