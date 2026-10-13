"""Resolução de identidade no WhatsApp por vínculo confirmado (PR1, passo 1.2).

A identidade de quem escreve no WhatsApp vem SOMENTE do vínculo confirmado por
TOTP (profiles.whatsapp_phone, conquistado em wa_link_service). O caminho
legado que aceitava phone ou emergency_contact_phone foi retirado da saúde:
telefone de contato e telefone de emergência não autorizam uma conta clínica,
e um número sem vínculo nunca é tratado como identidade.

Resultados possíveis (contrato estruturado do passo 1.3):
  ok          - exatamente uma conta vinculada (user_id, full_name)
  unlinked    - nenhuma conta vinculada a este número
  ambiguous   - mais de uma conta vinculada ao mesmo número: interrompe e
                pede regularização; o modelo nunca escolhe a identidade
  unavailable - indisponibilidade do banco
"""
from typing import Any, List, Optional

STATUS_OK = "ok"
STATUS_UNLINKED = "unlinked"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNAVAILABLE = "unavailable"


# Sem vínculo: orientação clara para vincular pelo app, sem prometer
# atendimento. A rede de emergência continua visível.
UNLINKED_REPLY = (
    "Aqui é o Braga Saúde. 💚\n\n"
    "Eu só consigo cuidar dos seus registros e da sua família quando seu "
    "WhatsApp está vinculado à sua conta — é assim que garanto que estou "
    "conversando com a pessoa certa.\n\n"
    "Para vincular, abra o aplicativo Braga Saúde e toque em 'Vincular "
    "WhatsApp'. Em 1 minuto está pronto. Se precisar de ajuda, escreva para "
    "contato@bragasaude.online.\n\n"
    "Se for uma emergência (falta de ar, dor forte no peito, desmaio), chame o "
    "SAMU 192 ou vá à UPA mais próxima agora."
)

# Ambiguidade: mais de uma conta aponta para o mesmo número. Não se escolhe
# uma (a identidade não vem da interpretação do modelo); interrompe e pede
# regularização.
AMBIGUOUS_REPLY = (
    "Encontrei mais de uma conta vinculada a este WhatsApp e não consigo saber "
    "qual é a sua. Por segurança, vou parar por aqui.\n\n"
    "Para regularizar e voltar a conversar comigo, escreva para "
    "contato@bragasaude.online informando este número.\n\n"
    "Se for uma emergência (falta de ar, dor forte no peito, desmaio), chame o "
    "SAMU 192 ou vá à UPA mais próxima agora."
)

# Indisponibilidade do banco: nunca é tratada como "conta não existe".
UNAVAILABLE_REPLY = (
    "Agora não consegui verificar sua conta no Braga Saúde. Tente novamente em "
    "instantes.\n\n"
    "Se for uma emergência (falta de ar, dor forte no peito, desmaio), chame o "
    "SAMU 192 ou vá à UPA mais próxima agora."
)


def normalize_phone_digits(phone: Optional[str]) -> str:
    """Dígitos do número — a forma como whatsapp_phone é comparado."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def reply_for_status(status: Optional[str]) -> str:
    """Mensagem institucional do estado do vínculo (ok não tem resposta)."""
    if status == STATUS_AMBIGUOUS:
        return AMBIGUOUS_REPLY
    if status == STATUS_UNAVAILABLE:
        return UNAVAILABLE_REPLY
    return UNLINKED_REPLY


async def resolve_linked_account(conn: Any, sender_phone: Optional[str]) -> dict:
    """
    Resolve a conta vinculada ao número do remetente.

    conn: conexão do Postgres. A chamadora controla a conexão/transação, assim
    a resolução pode ser usada na mesma transação de uma gravação (passo 1.4).
    Retorna {"status": ..., "user_id": ..., "full_name": ...}; em ambiguous
    também traz "accounts" para diagnóstico.
    """
    digits = normalize_phone_digits(sender_phone)
    if not digits:
        return {"status": STATUS_UNLINKED, "user_id": None, "full_name": None}
    try:
        rows: List[Any] = await conn.fetch(
            "SELECT id, full_name FROM profiles "
            "WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1",
            digits,
        )
    except Exception as err:
        print(f"[WA Identity] Falha ao resolver vínculo: {err}")
        return {"status": STATUS_UNAVAILABLE, "user_id": None, "full_name": None}

    if not rows:
        return {"status": STATUS_UNLINKED, "user_id": None, "full_name": None}
    if len(rows) > 1:
        return {
            "status": STATUS_AMBIGUOUS,
            "user_id": None,
            "full_name": None,
            "accounts": [str(r["id"]) for r in rows],
        }
    row = rows[0]
    return {
        "status": STATUS_OK,
        "user_id": str(row["id"]),
        "full_name": row["full_name"] or "",
    }


def is_confirmed(identity: Optional[dict]) -> bool:
    """True apenas para vínculo confirmado e inequívoco."""
    return bool(identity) and identity.get("status") == STATUS_OK
