"""Pedido explícito de mensagem, distinto de consultas sobre memória."""
import re
import unicodedata


def is_care_request(text):
    norm = ''.join(c for c in unicodedata.normalize('NFD', (text or '').casefold())
                   if not unicodedata.combining(c)).strip()
    norm = re.sub(r'^por favor[, ]*', '', norm)
    if re.search(r'^lembra (?:de mim|das minhas preferencias)\b', norm):
        return False
    return bool(re.search(
        r'^(?:(?:me\s+)?(?:lembra|lembre)\b|lembrete\s*:|'
        r'(?:manda|mande|envia|envie|avisa|avise)\b|'
        r'(?:voce\s+)?(?:pode|poderia|consegue)\s+(?:me\s+)?(?:lembrar|avisar|enviar|mandar)\b)', norm))
