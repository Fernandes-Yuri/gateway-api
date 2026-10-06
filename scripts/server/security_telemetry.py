"""Telemetria sem payload, fila limitada e sinais explicáveis. Nunca bloqueia."""
import asyncio
import base64
from contextvars import ContextVar
from datetime import datetime, timezone
import ipaddress
import hashlib
import hmac
import json
import os
import re
import time
import uuid

from starlette.requests import Request
from security_defense import get_client_ip

_event_state = ContextVar('security_event_state', default=None)
SIGNALS = {
    'normal': ('Requisição recebida', 0),
    'uid_mismatch': ('UID diverge da identidade autenticada', 8),
    'forbidden_resource': ('Acesso a recurso sem autorização', 8),
    'forbidden_fields': ('Campo reservado ao servidor no perfil', 8),
    'invalid_payload': ('Payload rejeitado pelo contrato', 2),
    'invalid_auth': ('Credencial inválida ou sessão expirada', 2),
    'auth_required': ('Autenticação necessária', 0),
    'authorization_denied': ('Operação não autorizada', 3),
    'not_found': ('Rota ou recurso não encontrado', 1),
    'rate_limited': ('Limite de requisições excedido', 3),
    'restricted_access': ('Conta com restrição ativa', 0),
    'server_error': ('Falha do serviço', 0),
}


def mark_signal(signal):
    state = _event_state.get()
    if state is not None and signal in SIGNALS:
        state['security_signal'] = signal


def signal_for(status, explicit=None):
    if explicit in SIGNALS:
        return explicit
    return {401: 'auth_required', 403: 'authorization_denied', 404: 'not_found',
            422: 'invalid_payload', 429: 'rate_limited'}.get(status, 'server_error' if status >= 500 else 'normal')


def canonical_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return 'unknown'


def safe_header(value, pattern, max_length):
    return value if value and len(value) <= max_length and re.fullmatch(pattern, value) else None


def portal_context(request):
    """Metadados do visitante assinados pelo portal, nunca UID fornecido pelo cliente."""
    value = request.headers.get('x-security-context', '')
    if not value or len(value) > 1500:
        return None
    try:
        encoded, signature = value.split('.')
        secret = os.getenv('SERVICE_TOKEN', '').strip()
        if not secret:
            return None
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if (data['path'] != request.url.path or data['method'] != request.method or
                not time.time() < data['exp'] <= time.time()+60):
            return None
        ip = data['ip']
        if not isinstance(ip, str) or (ip != 'unknown' and canonical_ip(ip) == 'unknown'):
            return None
        return {'ip': canonical_ip(ip),
                'country': safe_header(data.get('country'), r'[A-Z]{2}', 2),
                'region': safe_header(data.get('region'), r'[\w .-]+', 80)}
    except (ValueError, TypeError, KeyError):
        return None


class SecurityTelemetry:
    def __init__(self, pool_provider, capacity=2000):
        self.pool_provider = pool_provider
        self.queue = asyncio.Queue(maxsize=capacity)
        self.worker = None
        self.dropped = 0
        self.persisted = 0
        self.last_flush = None
        self.last_error = None
        self.retention_error = None

    def enqueue(self, event):
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def start(self):
        if self.worker and not self.worker.done():
            return
        self.worker = asyncio.create_task(self.run())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None
        try:
            async def drain():
                while not self.queue.empty():
                    await self.flush()
            await asyncio.wait_for(drain(), timeout=3)
        except asyncio.TimeoutError:
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
                self.dropped += 1

    async def flush(self):
        batch = []
        while len(batch) < 100 and not self.queue.empty():
            batch.append(self.queue.get_nowait())
        if not batch:
            return
        try:
            pool = self.pool_provider()
            if pool is None:
                raise RuntimeError('pool indisponível')
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany('''INSERT INTO security_request_events
                        (request_id,occurred_at,ip,user_id,country,region,route,method,status,duration_ms,signal,weight,app_version,ray_id,connection_ip,source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                        ON CONFLICT(request_id) DO NOTHING''', batch)
            self.persisted += len(batch)
            self.last_flush = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        except asyncio.CancelledError:
            self.dropped += len(batch)
            raise
        except Exception:
            # Não registra erro SQL, payload ou DSN. Perdas ficam explícitas no painel.
            self.last_error = 'Falha de persistência da telemetria.'
            self.dropped += len(batch)
        finally:
            for _ in batch:
                self.queue.task_done()

    async def run(self):
        last_retention = 0
        while True:
            await asyncio.sleep(2)
            try:
                await asyncio.wait_for(self.flush(), timeout=5)
            except asyncio.TimeoutError:
                self.last_error = 'Tempo limite na persistência da telemetria.'
            if time.monotonic() - last_retention > 3600:
                last_retention = time.monotonic()
                try:
                    pool = self.pool_provider()
                    if pool:
                        async def cleanup():
                            async with pool.acquire() as conn:
                                await conn.execute("DELETE FROM security_request_events WHERE occurred_at < NOW()-INTERVAL '7 days'")
                        await asyncio.wait_for(cleanup(), timeout=5)
                        self.retention_error = None
                except Exception:
                    self.retention_error = 'Falha na retenção da telemetria.'

    def health(self):
        return {'queued': self.queue.qsize(), 'capacity': self.queue.maxsize,
                'dropped': self.dropped, 'persisted': self.persisted,
                'lastFlush': self.last_flush, 'error': self.last_error or self.retention_error,
                'scope': 'Processo atual; fila não durável, retenção de 7 dias.'}


class SecurityTelemetryMiddleware:
    def __init__(self, app, telemetry):
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        request = Request(scope)
        context = portal_context(request)
        if scope['path'].startswith('/api/admin/') and not (context and scope['path'] in ('/api/admin/login', '/api/admin/login-step2')):
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        token = _event_state.set(state)
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc)
        request_id = uuid.uuid4()
        status = 500

        async def tracked_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message['headers'] = list(message.get('headers', [])) + [(b'x-request-id', str(request_id).encode())]
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            status = 500
            raise
        finally:
            signal = signal_for(status, state.get('security_signal'))
            if context and status == 401 and scope['path'].startswith('/api/admin/login'):
                signal = 'invalid_auth'
            route = getattr(scope.get('route'), 'path', None) or '/rota-nao-identificada'
            connection_ip = canonical_ip(get_client_ip(request))
            # Somente templates de rota: nunca caminho com UID, nomes de arquivos ou query.
            self.telemetry.enqueue((request_id, timestamp, context['ip'] if context else connection_ip,
                state.get('security_verified_uid'),
                context['country'] if context else safe_header(request.headers.get('cf-ipcountry'), r'[A-Z]{2}', 2),
                context['region'] if context else safe_header(request.headers.get('cf-region'), r'[\w .-]+', 80),
                route[:160], scope['method'][:10], status,
                min(2147483647, int((time.monotonic()-started)*1000)), signal, SIGNALS[signal][1],
                safe_header(request.headers.get('x-app-version'), r'[0-9A-Za-z.+_-]+', 32),
                safe_header(request.headers.get('cf-ray'), r'[a-fA-F0-9]+-[A-Z]{3}', 80),
                connection_ip, 'portal' if context else 'gateway'))
            _event_state.reset(token)


def install(app, pool_provider):
    telemetry = SecurityTelemetry(pool_provider)
    app.add_middleware(SecurityTelemetryMiddleware, telemetry=telemetry)
    app.on_event('startup')(telemetry.start)
    app.on_event('shutdown')(telemetry.stop)
    return telemetry
