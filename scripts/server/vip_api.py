"""Public write-only waitlist in the existing PostgreSQL database."""
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)
SUCCESS = {'success': True, 'message': 'Inscrição recebida! Você receberá o convite quando lançarmos.'}


def normalize_signup(data):
    if not isinstance(data, dict):
        raise ValueError('Informe nome e e-mail.')
    name, email = data.get('fullName'), data.get('email')
    if not isinstance(name, str) or not isinstance(email, str):
        raise ValueError('Informe nome e e-mail.')
    name, email = name.strip(), email.strip().lower()
    if not 1 <= len(name) <= 255 or any(ord(c) < 32 for c in name):
        raise ValueError('Informe um nome válido com até 255 caracteres.')
    if len(email) > 255 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('Informe um e-mail válido com até 255 caracteres.')
    return name, email


class VipRateLimit:
    # Single-process homelab: bounded memory and a global cap even behind a proxy.
    # No Redis/service added. Limits reset when braga-gateway restarts.
    def __init__(self):
        self.hits = deque()
        self.lock = asyncio.Lock()

    async def check(self, peer):
        async with self.lock:
            now = time.monotonic()
            while self.hits and self.hits[0][0] <= now - 600:
                self.hits.popleft()
            if sum(t > now - 60 for t, _ in self.hits) >= 60 or sum(p == peer for _, p in self.hits) >= 10:
                raise HTTPException(429, 'Aguarde alguns minutos antes de tentar novamente.', headers={'Retry-After': '600'})
            self.hits.append((now, peer))


def install_vip_routes(app, get_pool, notify_admin=None, send_confirmation_email=None):
    limiter = VipRateLimit()
    origins = set(os.getenv('VIP_ALLOWED_ORIGINS', 'https://braga-saude.web.app,https://braga-saude.firebaseapp.com').split(','))
    origins = {origin.strip().rstrip('/') for origin in origins if origin.strip()}

    @app.post('/api/public/vip-signups')
    async def signup(request: Request):
        origin = request.headers.get('origin')
        if origin is not None and origin not in origins:
            raise HTTPException(403, 'Origem não autorizada.')
        # Do not trust arbitrary X-Forwarded-For headers supplied by visitors.
        await limiter.check(request.client.host if request.client else 'unknown')
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise HTTPException(415, 'Envie os dados como JSON.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, 'Formulário muito grande.')
        try:
            name, email = normalize_signup(json.loads(body))
        except (ValueError, UnicodeDecodeError) as error:
            raise HTTPException(422, 'Confira o nome e o e-mail informados.') from error
        pool = get_pool()
        if pool is None:
            raise HTTPException(503, 'Cadastro temporariamente indisponível. Tente novamente.')
        is_new = False
        try:
            async with pool.acquire() as conn:
                # UNIQUE + ON CONFLICT is atomic, including simultaneous requests.
                # Existing names are not overwritten; response never reveals membership.
                tag = await conn.fetchval(
                    'INSERT INTO vip_signups(full_name,email) VALUES($1,$2) '
                    'ON CONFLICT(email) DO NOTHING RETURNING (xmax = 0) AS inserted',
                    name, email)
                is_new = bool(tag)
        except Exception as error:
            log.warning('VIP persistence failed (%s)', type(error).__name__)
            raise HTTPException(503, 'Cadastro temporariamente indisponível. Tente novamente.') from error

        # doc 10 §4.4: avisa o admin por WhatsApp e confirma por e-mail — mas só
        # numa inscrição NOVA. Repetição do mesmo e-mail não renotifica. Tudo
        # best-effort: a inscrição já está persistida.
        if is_new:
            if notify_admin is not None:
                try:
                    await notify_admin(
                        f'[Braga VIP] Nova inscrição: {name} <{email}>. '
                        f'Entre em contato para o convite.')
                except Exception as error:
                    log.warning('VIP admin notify failed (%s)', type(error).__name__)
            if send_confirmation_email is not None:
                try:
                    await send_confirmation_email(email, name)
                except Exception as error:
                    log.warning('VIP confirmation email failed (%s)', type(error).__name__)
        return SUCCESS

    @app.get('/api/public/vip-signups/health')
    async def health():
        pool = get_pool()
        if pool is None:
            raise HTTPException(503, 'Cadastro indisponível.')
        try:
            async with pool.acquire() as conn:
                await conn.execute('SELECT 1 FROM vip_signups LIMIT 0')
        except Exception as error:
            raise HTTPException(503, 'Cadastro indisponível.') from error
        return {'status': 'ready', 'vip_contract': 1}
