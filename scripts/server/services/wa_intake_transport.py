"""I/O dos provedores; sem URLs ou credenciais no contrato público."""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_MEDIA = 16 * 1024 * 1024
MAX_IMAGE = 5 * 1024 * 1024


class TransportFailure(Exception):
    def __init__(self, code, *, retryable=False, uncertain=False):
        self.code, self.retryable, self.uncertain = code, retryable, uncertain
        super().__init__(code)


def meta_config():
    version = os.getenv('WHATSAPP_API_VERSION', 'v21.0')
    receiver = os.getenv('WHATSAPP_PHONE_NUMBER_ID', '')
    token = os.getenv('WHATSAPP_TOKEN', '')
    if not receiver or not token:
        raise TransportFailure('meta_not_configured')
    return f'https://graph.facebook.com/{version}', receiver, {'Authorization': f'Bearer {token}'}


async def download_media(media_id):
    base, _, headers = meta_config()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f'{base}/{media_id}', headers=headers)
        if response.status_code != 200:
            raise TransportFailure('media_lookup_failed', retryable=response.status_code >= 500)
        metadata = response.json()
        url = metadata.get('url', '')
        host = urlparse(url).hostname or ''
        # A credencial só pode sair para o CDN oficial. Não seguir redirects.
        if urlparse(url).scheme != 'https' or not (host.endswith('.facebook.com') or host.endswith('.fbcdn.net') or host == 'lookaside.fbsbx.com'):
            raise TransportFailure('media_url_invalid')
        content = bytearray()
        async with client.stream('GET', url, headers=headers) as response:
            if response.status_code != 200:
                raise TransportFailure('media_download_failed', retryable=True)
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_MEDIA:
                    raise TransportFailure('media_too_large')
        return bytes(content), metadata.get('mime_type', '').split(';')[0].lower()


async def validate_media(content, mime, kind):
    if kind == 'image':
        valid = ((mime == 'image/jpeg' and content.startswith(b'\xff\xd8\xff')) or
                 (mime == 'image/png' and content.startswith(b'\x89PNG\r\n\x1a\n')))
        if not valid or len(content) > MAX_IMAGE:
            raise TransportFailure('image_invalid')
        return
    if not content or len(content) > MAX_MEDIA or mime not in ('audio/ogg', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr'):
        raise TransportFailure('audio_invalid')
    binary = shutil.which(os.getenv('WHATSAPP_FFPROBE_BIN', 'ffprobe'))
    if not binary:
        raise TransportFailure('ffprobe_required')
    with tempfile.TemporaryDirectory(prefix='braga-audio-') as directory:
        path = Path(directory) / 'upload'
        path.write_bytes(content)
        process = await asyncio.create_subprocess_exec(
            binary, '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,channels:format=duration',
            '-of', 'json', str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
    try:
        info = json.loads(stdout)
        streams = info['streams']
        valid = process.returncode == 0 and len(streams) == 1 and streams[0]['codec_type'] == 'audio'
        valid &= 0 < float(info['format']['duration']) <= 600
        codecs = {'audio/ogg': {'opus'}, 'audio/mpeg': {'mp3'}, 'audio/mp4': {'aac'},
                  'audio/aac': {'aac'}, 'audio/amr': {'amr_nb', 'amr_wb'}}
        valid &= streams[0]['codec_name'] in codecs[mime]
        if mime == 'audio/ogg':
            valid &= streams[0].get('channels') == 1
        if not valid:
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError):
        raise TransportFailure('audio_invalid') from None


async def transcribe(content, mime):
    from config import GROQ_API_KEY
    GROQ_API_KEY_WHATSAPP=os.getenv('GROQ_API_KEY_WHATSAPP') or GROQ_API_KEY
    if not GROQ_API_KEY_WHATSAPP:
        raise TransportFailure('groq_not_configured')
    if mime in ('audio/aac', 'audio/amr'):
        binary = shutil.which(os.getenv('WHATSAPP_FFMPEG_BIN', 'ffmpeg'))
        if not binary:
            raise TransportFailure('ffmpeg_required')
        with tempfile.TemporaryDirectory(prefix='braga-transcribe-') as directory:
            source, target = Path(directory) / 'input', Path(directory) / 'audio.wav'
            source.write_bytes(content)
            process = await asyncio.create_subprocess_exec(binary, '-v', 'error', '-nostdin', '-i', str(source),
                '-ac', '1', '-ar', '16000', str(target), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), 30)
            except (TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                raise
            if process.returncode or not target.exists():
                raise TransportFailure('audio_conversion_failed')
            content, mime = target.read_bytes(), 'audio/wav'
    extension = {'audio/wav': 'wav', 'audio/ogg': 'ogg', 'audio/mpeg': 'mp3', 'audio/mp4': 'm4a',
                 'audio/aac': 'aac', 'audio/amr': 'amr'}[mime]
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post('https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY_WHATSAPP}'},
            files={'file': (f'audio.{extension}', content, mime)},
            data={'model': 'whisper-large-v3-turbo', 'language': 'pt', 'response_format': 'json'})
        if response.status_code != 200:
            raise TransportFailure('transcription_failed')
        return str(response.json().get('text', '')).strip()[:12000]


async def send(phone, text='', *, content=None, mime=None, media_id=None, buttons=None):
    base, receiver, headers = meta_config()
    async with httpx.AsyncClient(timeout=30) as client:
        if buttons:
            action_buttons = []
            for btn in buttons[:3]:
                action_buttons.append({
                    "type": "reply",
                    "reply": {
                        "id": btn["id"],
                        "title": btn["title"][:20]
                    }
                })
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text},
                    "action": {"buttons": action_buttons}
                }
            }
        elif content is not None and not media_id:
            try:
                upload = await client.post(f'{base}/{receiver}/media', headers=headers,
                    data={'messaging_product': 'whatsapp', 'type': mime}, files={'file': ('audio.' + {'audio/ogg':'ogg','audio/mpeg':'mp3','audio/mp4':'m4a','audio/aac':'aac','audio/amr':'amr'}[mime], content, mime)})
                if upload.status_code not in (200, 201):
                    raise TransportFailure('media_upload_failed', retryable=upload.status_code >= 500)
                media_id = upload.json()['id']
            except httpx.HTTPError:
                # O upload não envia mensagem para o paciente; pode ser repetido.
                raise TransportFailure('media_upload_failed', retryable=True) from None
        if not buttons:
            payload = {'messaging_product': 'whatsapp', 'to': phone,
                       'type': 'audio' if media_id else 'text'}
            payload[payload['type']] = {'id': media_id} if media_id else {'body': text, 'preview_url': False}
        try:
            response = await client.post(f'{base}/{receiver}/messages', headers=headers, json=payload)
        except httpx.HTTPError:
            raise TransportFailure('send_uncertain', uncertain=True) from None
        if response.status_code not in (200, 201):
            # Somente rejeição explícita por quota é repetida automaticamente.
            raise TransportFailure('meta_rejected', retryable=response.status_code == 429,
                                   uncertain=response.status_code >= 500)
        try:
            return response.json()['messages'][0]['id']
        except (ValueError, KeyError, IndexError, TypeError):
            raise TransportFailure('send_uncertain', uncertain=True) from None
