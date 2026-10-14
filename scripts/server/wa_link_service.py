"""Reconhecimento e execucao do vinculo WhatsApp<->conta no webhook.

Este modulo e a ponte entre a UI (botao que abre wa.me com o codigo TOTP) e o
gateway. Ele:
  1. detecta que a mensagem recebida e uma tentativa de vinculo;
  2. recomputa o codigo TOTP da conta candidata;
  3. vincula o numero (verificado pela Meta) a conta;
  4. registra a tentativa para auditoria e cap de retentativas.

Arquitetura temporaria, ate a Meta aprovar o template de autenticacao.
"""
from typing import Any, Optional, Tuple  # AUD-G(B): Dict morto

from wa_link_totp import (
    is_six_digit_code,
    verify_totp_code,
)

# Limite de retentativas antes de parar de ajudar (janela deslizante).
LINK_ATTEMPT_LIMIT = 3
LINK_ATTEMPT_WINDOW_SECONDS = 10 * 60

# Status registrados na tabela whatsapp_link_attempts.
STATUS_SUCCESS = "SUCCESS"
STATUS_WRONG = "WRONG_CODE"
STATUS_EXPIRED = "EXPIRED"
STATUS_BLOCKED = "BLOCKED"

_MSG_WRONG = (
    "Olá! Recebi seu código, mas ele não é válido. Os códigos do Braga Saúde valem "
    "por 3 minutos. Volte no aplicativo, toque em 'Vincular WhatsApp' de novo e me "
    "mande o código novo que aparecer. 💚"
)

_MSG_SUCCESS = (
    "Tudo certo! Seu WhatsApp já está vinculado à sua conta do Braga Saúde. "
    "A partir de agora eu te reconheço aqui. 💚"
)

_MSG_BLOCKED = (
    "Você já tentou várias vezes e não consegui reconhecer os códigos. Por "
    "segurança, vou pedir que você recomece daqui a alguns minutos. Se precisar de "
    "ajuda, é só falar com a gente no aplicativo. 💚"
)


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in phone or "" if ch.isdigit())


def looks_like_link_attempt(text: str) -> bool:
    """
    Heuristica barata e deterministica: a mensagem e so o codigo de 6 digitos,
    opcionalmente antecedido por uma palavra-convite ("Vincular Braga", "código").
    Nao chama LLM de proposito: esse fork e deterministico.
    """
    if not text:
        return False
    t = text.strip()
    if is_six_digit_code(t):
        return True
    # "Vincular Braga Saude 123456" / "codigo 123456"
    lowered = t.lower()
    if any(w in lowered for w in ("vincular", "código", "codigo", "braga")):
        digits = "".join(ch for ch in t if ch.isdigit())
        if len(digits) >= 6:
            return True
    return False


def _extract_code(text: str) -> str:
    t = text.strip()
    if is_six_digit_code(t):
        return t
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


async def _recent_wrong_attempts(conn, sender_digits: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS n
        FROM whatsapp_link_attempts
        WHERE sender_phone = $1
          AND status IN ($2, $3)
          AND created_at > NOW() - ($4 || ' seconds')::INTERVAL
        """,
        sender_digits, STATUS_WRONG, STATUS_BLOCKED, str(LINK_ATTEMPT_WINDOW_SECONDS),
    )
    return int(row["n"]) if row else 0


async def _log_attempt(conn, sender_digits: str, code: str,
                       matched_user: Optional[str], status: str) -> None:
    await conn.execute(
        """
        INSERT INTO whatsapp_link_attempts (sender_phone, code_received, matched_user, status)
        VALUES ($1, $2, $3, $4)
        """,
        sender_digits, code, matched_user, status,
    )


async def _find_account_by_code(conn, code: str) -> Optional[Tuple[str, str]]:
    """
    Acha a conta cujo codigo TOTP atual bate. Como o codigo e deterministico por
    conta+tempo, percorre os segredos existentes; no MVP com poucas contas isso e
    barato e direto. Retorna (user_id, full_name).

    Nao usa indice codigo->conta justamente para evitar a colisao de duas contas
    exibindo o mesmo digito na mesma janela: quem decide e o segredo, nao o codigo.
    """
    rows = await conn.fetch(
        "SELECT id, full_name, totp_secret FROM profiles WHERE totp_secret IS NOT NULL"
    )
    for r in rows:
        try:
            if verify_totp_code(r["totp_secret"], code):
                return r["id"], r["full_name"] or "amigo(a)"
        except Exception as row_err:
            print(f"[WhatsApp Link] Falha ao verificar código: {type(row_err).__name__}")
            continue
    return None


async def handle_link_attempt(
    sender_phone: str,
    text: str,
    db_pool: Any,
    send_text: Any,
) -> bool:
    """
    Tenta processar a mensagem como vinculo. Retorna True se consumiu a mensagem
    (ja respondeu e o webhook nao deve seguir pro fluxo normal).

    db_pool/send_text sao injetados: facilita testar sem rede nem WhatsApp real.
    """
    if not looks_like_link_attempt(text):
        return False
    if db_pool is None:
        return False

    sender_digits = _normalize_phone(sender_phone)
    code = _extract_code(text)

    async with db_pool.acquire() as conn:
        # 1. Cap de retentativas: protege contra spray e contra loop de frustracao.
        wrong = await _recent_wrong_attempts(conn, sender_digits)
        if wrong >= LINK_ATTEMPT_LIMIT:
            await _log_attempt(conn, sender_digits, code, None, STATUS_BLOCKED)
            await send_text(sender_phone, _MSG_BLOCKED)
            return True

        # 2. Ja vinculado? Responde com sucesso em vez de reprocessar.
        already = await conn.fetchrow(
            "SELECT id, full_name FROM profiles WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1",
            sender_digits,
        )
        if already:
            await _log_attempt(
                conn, sender_digits, code, already["id"],
                f"{STATUS_SUCCESS}_ALREADY",
            )
            await send_text(
                sender_phone,
                f"Seu WhatsApp já está vinculado à conta de {already['full_name'] or 'amigo(a)'}. "
                "Não precisa fazer de novo! 💚",
            )
            return True

        # 3. Compara contra o codigo atual de cada conta (janela unica, sem +/-1).
        match = await _find_account_by_code(conn, code)
        if match is None:
            await _log_attempt(conn, sender_digits, code, None, STATUS_WRONG)
            await send_text(sender_phone, _MSG_WRONG)
            return True

        user_id, user_name = match

        # 4. Vincula: o numero veio verificado pela Meta (campo from do webhook).
        #    Unicidade: revogua vinculo anterior do mesmo numero em outra conta.
        await conn.execute(
            "UPDATE profiles SET whatsapp_phone = NULL, whatsapp_linked_at = NULL "
            "WHERE whatsapp_phone = $1 AND id <> $2",
            sender_digits, user_id,
        )
        await conn.execute(
            "UPDATE profiles SET whatsapp_phone = $1, whatsapp_linked_at = NOW() WHERE id = $2",
            sender_digits, user_id,
        )
        await _log_attempt(conn, sender_digits, code, user_id, STATUS_SUCCESS)
        await send_text(sender_phone, _MSG_SUCCESS)
        print("[WhatsApp Link] Vínculo confirmado.")
        return True
