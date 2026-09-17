"""Verificacao de assinatura do webhook da Meta Cloud API.

A Meta envia X-Hub-Signature-256 = HMAC-SHA256 do corpo bruto da
requisicao usando o App Secret do WhatsApp. Sem essa verificacao, qualquer
atacante pode forjar payloads do webhook e se passar por qualquer
remetente — incluindo confirmacoes de registros de saude (takeover).

O App Secret vem exclusivamente de variavel de ambiente (WHATSAPP_APP_SECRET).
"""
import hashlib
import hmac
import os


def get_app_secret() -> str | None:
    return os.getenv("WHATSAPP_APP_SECRET")


def verify_meta_signature(
    signature_header: str | None, raw_body: bytes
) -> bool:
    """Verifica X-Hub-Signature-256 contra o corpo bruto da requisicao.

    Retorna True somente se a assinatura for valida e o secret estiver
    configurado. Comparacao em tempo constante (hmac.compare_digest).
    """
    secret = get_app_secret()
    if not secret:
        # Sem secret configurado, o webhook NAO e confiavel.
        return False
    if not signature_header:
        return False

    # Formato esperado: "sha256=<hex>"
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)
