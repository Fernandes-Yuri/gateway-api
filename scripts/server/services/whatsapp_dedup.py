"""Antiduplicidade de mensagens e operações do WhatsApp (PR1, passo 1.4).

A Meta reenvia mensagens; sem persistência, o reenvio reprocessava tudo (nova
pendência, novo registro de saúde, dose dobrada). A recepção e as chaves de
operação agora são registradas no banco (migration 051), de modo que um
reenvio ou um restart de processo nunca reprocessa nem duplica.

Uso:
  - claim_message_receipt: no início do processamento de cada mensagem Meta.
    True = inédita (processe); False = reenvio (ignore).
  - claim_operation_key: na mesma transação da gravação efetiva. True =
    reservada (execute); False = a operação já foi efetivada (reenvio).
"""
from typing import Any, Optional


def operation_key_for(message_id: Optional[str], kind: str) -> str:
    """Chave canônica de uma operação: tipo + id da mensagem Meta."""
    return f"{kind}:{message_id or 'no-msg-id'}"


async def claim_message_receipt(conn: Any, message_id: Optional[str],
                                user_id: Optional[str] = None) -> bool:
    """
    Registra a mensagem como processada de forma atômica.

    INSERT ... ON CONFLICT DO NOTHING decide a corrida entre dois reenvios
    concorrentes: só a primeira vence. Retorna True se inédita.
    """
    if not message_id:
        # Sem id (payload anômalo): não há o que deduplicar.
        return True
    try:
        row = await conn.fetchrow(
            """INSERT INTO whatsapp_message_receipts (message_id, user_id)
               VALUES ($1, $2)
               ON CONFLICT (message_id) DO NOTHING
               RETURNING message_id""",
            message_id, user_id,
        )
    except Exception as err:
        # Tabela ausente (migration não aplicada): não bloqueia o fluxo de
        # mensagens — a dedup volta a ser best-effort, como antes.
        print(f"[WA Dedup] Aviso: receipt não registrado ({message_id}): {err}")
        return True
    return row is not None


async def claim_operation_key(conn: Any, *, message_id: Optional[str],
                              kind: str, actor_id: str) -> bool:
    """
    Reserva a chave de operação (message_id + tipo) para o ator.

    Deve ser chamada NA MESMA TRANSAÇÃO da gravação efetiva: ou a operação e a
    chave entram juntas, ou nenhuma entra. Reenvio posterior encontra a chave e
    retorna False — a operação já foi efetivada, não duplica.
    """
    if not actor_id:
        return False
    try:
        row = await conn.fetchrow(
            """INSERT INTO whatsapp_operation_keys (message_id, operation_key, actor_id)
               VALUES ($1, $2, $3)
               ON CONFLICT (operation_key) DO NOTHING
               RETURNING id""",
            message_id or "", operation_key_for(message_id, kind), actor_id,
        )
    except Exception as err:
        print(f"[WA Dedup] Aviso: chave de operação não reservada ({kind}): {err}")
        # Sem garantia de unicidade, é mais seguro NÃO efetivar do que duplicar.
        return False
    return row is not None
