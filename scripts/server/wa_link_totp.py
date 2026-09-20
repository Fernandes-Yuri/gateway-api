"""Vinculo de WhatsApp por TOTP (arquitetura temporaria, ate a Meta liberar template).

Cada conta nasce com um segredo TOTP gerado sob o capo. O app calcula o codigo
atual localmente, no ato do clique, sem chamar o servidor. O webhook recomputa o
mesmo codigo a partir do segredo guardado na conta e, se bater, vincula o numero
(que veio verificado pela Meta) aquela conta.

Janela de 180s, com tolerancia de +/-1 janela adjacente: um relogio de celular
descompassado em alguns minutos nao bloqueia o vinculo; codigos a mais de uma
janela de distancia continuam rejeitados (o usuario volta no app e pega um novo).
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
    True se o codigo bater com a janela atual ou com as janelas adjacentes (+/-1
    step), tolerando drift de relogio entre o celular do usuario e o servidor.
    Codigos a mais de uma janela de distancia sao rejeitados.
    """
    if not secret_b32 or not code:
        return False
    now = time.time() if at is None else at
    base_counter = int(now // TOTP_STEP_SECONDS)
    submitted = str(code).strip().zfill(TOTP_DIGITS)
    # D-WA1: aceita a janela atual e as vizinhas; comparacao em tempo constante em
    # cada tentativa (nao vaza progresso por timing).
    for step in (base_counter - 1, base_counter, base_counter + 1):
        if hmac.compare_digest(submitted, _hotp(secret_b32, step)):
            return True
    return False


def seconds_until_rotation(at: float = None) -> int:
    """Segundos restantes da janela atual (util para o app/UX saber)."""
    now = time.time() if at is None else at
    return TOTP_STEP_SECONDS - int(now % TOTP_STEP_SECONDS)


def is_six_digit_code(text: str) -> bool:
    """True se o texto e exatamente 6 digitos (o que o bot espera receber)."""
    return bool(text) and len(text.strip()) == 6 and text.strip().isdigit()
