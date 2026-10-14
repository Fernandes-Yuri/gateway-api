"""Endereço oficial com Open Graph e detecção de links de instalação."""
import re
from urllib.parse import urlsplit

DOWNLOAD_URL = 'https://bragasaude.online/'
DOWNLOAD_REPLY = (
    f'O Braga Saúde para Android está disponível na página oficial: {DOWNLOAD_URL}\n'
    'Lá você encontra o download e as instruções de instalação.')


def has_download_link(text):
    for raw in re.findall(r'https?://[^\s<>]+', text or '', re.IGNORECASE):
        try:
            url = urlsplit(raw.rstrip('.,;:!?)]}\"\''))
        except ValueError:
            continue
        host = (url.hostname or '').casefold()
        if host in ('bragasaude.online', 'www.bragasaude.online') and url.path in ('', '/', '/app-download.html'):
            return True
        if host == 'api.bragasaude.online' and url.path == '/api/app/download':
            return True
    return False
