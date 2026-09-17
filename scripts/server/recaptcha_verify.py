"""Validacao server-side de tokens do Google reCAPTCHA.

O front emite o token via RecaptchaVerifier (Firebase) e o envia no campo
`recaptchaToken`. A verificacao SO e confiavel no servidor: qualquer
cheque no cliente e burlavel. Este modulo faz a chamada siteverify do
Google e trata os modos de falha de forma explicita.

Modos:
  - RECAPTCHA_SECRET ausente      -> retorna {'valid': False, 'reason': 'disabled'}
    (nunca aprova; o chamador decide como agir)
  - token ausente/invalido        -> {'valid': False, ...}
  - score baixo / acao incorreta  -> {'valid': False, ...}
"""
import urllib.parse
import urllib.request
import json
import logging

logger = logging.getLogger("braga.recaptcha")

from config import RECAPTCHA_SECRET, RECAPTCHA_VERIFY_URL


def verify_recaptcha(
    token: str | None,
    expected_action: str | None = None,
    min_score: float = 0.5,
    remote_ip: str | None = None,
) -> dict:
    """Verifica um token reCAPTCHA v2/v3 no Google.

    Retorna dict com 'valid' (bool) e 'reason' (str).
    """
    if not RECAPTCHA_SECRET:
        logger.warning(
            "RECAPTCHA_SECRET nao configurado; verificacao desativada "
            "(tokens NAO sao aprovados por padrao)."
        )
        return {"valid": False, "reason": "disabled"}

    if not token or not isinstance(token, str):
        return {"valid": False, "reason": "missing_token"}

    payload = {"secret": RECAPTCHA_SECRET, "response": token.strip()}
    if remote_ip:
        payload["remoteip"] = remote_ip

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(RECAPTCHA_VERIFY_URL, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as err:
        logger.warning("Falha ao contatar o Google siteverify: %s", err)
        return {"valid": False, "reason": "verify_unavailable"}

    if not result.get("success", False):
        errors = result.get("error-codes", [])
        logger.info("reCAPTCHA rejeitado: %s", errors)
        return {"valid": False, "reason": "rejected", "errors": errors}

    # reCAPTCHA v3 retorna score/açao; v2 nao retorna esses campos.
    score = result.get("score")
    action = result.get("action")
    if score is not None and score < min_score:
        logger.info("reCAPTCHA score baixo: %.2f < %.2f", score, min_score)
        return {"valid": False, "reason": "low_score", "score": score}
    if expected_action and action and action != expected_action:
        logger.info("reCAPTCHA action incorreto: %s != %s", action, expected_action)
        return {"valid": False, "reason": "action_mismatch", "action": action}

    return {"valid": True, "score": score, "action": action}
