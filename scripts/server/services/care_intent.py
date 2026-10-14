"""Pedido explícito de mensagem, distinto de consultas sobre memória."""
import re
import unicodedata


def is_care_request(text):
    norm = ''.join(c for c in unicodedata.normalize('NFD', (text or '').casefold())
                   if not unicodedata.combining(c)).strip()
    norm = re.sub(r'^por favor[, ]*', '', norm)
    if re.search(r'^(?:me\s+)?lembra (?:de mim|das minhas preferencias)\b', norm):
        return False
    return bool(re.search(
        r'^(?:(?:me\s+)?(?:lembra|lembre)\b|lembrete\s*:|'
        r'(?:manda|mande|envia|envie|avisa|avise)\b|'
        r'(?:voce\s+)?(?:pode|poderia|consegue)\s+(?:me\s+)?(?:lembrar|avisar|enviar|mandar)\b)', norm))


REMINDER_APP_REPLY = (
    "Configure esse lembrete no aplicativo Braga Saúde. "
    "Assim você acompanha sua rotina e recebe os lembretes pelo app.")


def is_reminder_request(text):
    """Lembretes pertencem ao app; envio explícito imediato é outro fluxo."""
    norm = ''.join(c for c in unicodedata.normalize('NFD', (text or '').casefold())
                   if not unicodedata.combining(c)).strip()
    norm = re.sub(r'^por favor[, ]*', '', norm)
    if re.search(r'^(?:(?:voce\s+)?(?:pode|poderia|consegue)\s+)?me\s+(?:lembr\w*|avis\w*|notific\w*)\b', norm) and re.search(
            r'\b(?:a cada|de hora em hora|todos os dias|diariamente)\b', norm):
        return True
    if re.search(r'^(?:crie|criar|configure|configurar|agende|agendar|quero|preciso)\b.*\blembretes?\b', norm):
        return True
    if not is_care_request(text):
        return False
    return bool(re.search(
        r'^(?:(?:me\s+)?(?:lembra|lembre)\b|lembrete\s*:|'
        r'(?:voce\s+)?(?:pode|poderia|consegue)\s+(?:me\s+)?lembrar\b)', norm))
