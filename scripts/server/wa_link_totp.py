"""Vinculo de WhatsApp por TOTP (arquitetura temporaria, ate a Meta liberar template).

Cada conta nasce com um segredo TOTP gerado sob o capo. O app calcula o codigo
atual localmente, no ato do clique, sem chamar o servidor. O webhook recomputa o
mesmo codigo a partir do segredo guardado na conta e, se bater, vincula o numero
(que veio verificado pela Meta) aquela conta.

Janela de 180s, SEM tolerancia de janelas adjacentes: ou esta dentro, ou o usuario
volta no app e pega um codigo novo.
"""
import base64
import hmac
import hashlib
import os
import secrets
import time

# RFC 6238 / RFC 4648 padrao. Tamanho 20 bytes = segredo TOTP classico.
TOTP_SECRET_BYTES = 20
TOTP_DIGITS = 6
TOTP_STEP_SECONDS = 180


def generate_totp_secret() -> str:
    """Segredo TOTP aleatorio, base32, como o Google Authenticator espera."""
    raw = secrets.token_bytes(TOTP_SECRET_BYTES)
    return base64.b32encode(raw).decode("ascii")


def _hotp(secret_b32: str, counter: int, digits: int = TOTP_DIGITS) -> str:
    key = base64.b32decode(secret_b32, casefold=True)
    counter_bytes = counter.to_bytes(8, byteorder="big")
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return f"{code_int % (10 ** digits):0{digits}d}"


def current_totp_code(secret_b32: str, at: float = None) -> str:
    """Codigo TOTP atual para o segredo (o app e o servidor devem concordar)."""
    now = time.time() if at is None else at
    counter = int(now // TOTP_STEP_SECONDS)
    return _hotp(secret_b32, counter)


def verify_totp_code(secret_b32: str, code: str, at: float = None) -> bool:
    """
    True apenas se o codigo bate com a janela atual. Sem +/-1 janela: codigo
    velho ou adiantado e rejeitado, e o usuario deve voltar no app.
    """
    if not secret_b32 or not code:
        return False
    expected = current_totp_code(secret_b32, at=at)
    # Comparacao em tempo constante: nao vaza progresso por timing.
    return hmac.compare_digest(str(code).strip().zfill(TOTP_DIGITS), expected)


def seconds_until_rotation(at: float = None) -> int:
    """Segundos restantes da janela atual (util para o app/UX saber)."""
    now = time.time() if at is None else at
    return TOTP_STEP_SECONDS - int(now % TOTP_STEP_SECONDS)


def is_six_digit_code(text: str) -> bool:
    """True se o texto e exatamente 6 digitos (o que o bot espera receber)."""
    return bool(text) and len(text.strip()) == 6 and text.strip().isdigit()
