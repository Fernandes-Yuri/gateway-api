"""Sinais, privacidade, compatibilidade e perdas explícitas da coleta."""
import unittest
import base64
import hashlib
import hmac
import json
import time
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from api_identity import require_owner, firebase_uid
from routers.sync import ProfileSyncDto
from security_telemetry import SecurityTelemetry, install, mark_signal
import test_portal_auth_security as auth_tests
from routers import admin


class TelemetryRequests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.telemetry = install(self.app, lambda: None)

        @self.app.post('/api/sync/profile')
        async def profile(body: ProfileSyncDto, request: Request):
            request.state.security_verified_uid = 'uid_autenticado'
            require_owner('uid_autenticado', body.id)
            return {'success': True}

        @self.app.get('/api/items/{item_id}')
        async def item(item_id: str):
            return {'success': True}

        self.client = TestClient(self.app)

    def test_falha_de_infraestrutura_nao_acusa_credencial(self):
        @self.app.get('/api/authenticated')
        async def authenticated(request: Request):
            return {'uid': await firebase_uid(request)}

        firebase = ModuleType('firebase_admin')
        firebase._apps = {'teste': object()}
        firebase.auth = MagicMock()
        firebase.auth.verify_id_token.return_value = {'uid': 'uid_autenticado', 'auth_time': 1}
        with patch.dict('sys.modules', {'firebase_admin': firebase}), \
                patch('user_moderation.require_not_restricted', new=AsyncMock(side_effect=RuntimeError('indisponível'))):
            response = self.client.get('/api/authenticated', headers={'authorization': 'Bearer token-ficticio'})
        self.assertEqual(response.status_code, 503)
        event = self.event()
        self.assertEqual(event[3], 'uid_autenticado')
        self.assertEqual(event[10:12], ('server_error', 0))

    def event(self):
        return self.telemetry.queue.get_nowait()

    def test_uid_divergente_e_identidade_comprovada(self):
        response = self.client.post('/api/sync/profile', json={'id': 'outro_uid', 'fullName': 'Paciente'})
        self.assertEqual(response.status_code, 403)
        event = self.event()
        self.assertEqual(event[3], 'uid_autenticado')
        self.assertEqual(event[10:12], ('uid_mismatch', 8))
        self.assertNotIn('outro_uid', repr(event))

    def test_campos_reservados_recusados_sem_registrar_valores(self):
        response = self.client.post('/api/sync/profile', json={'id': 'uid_autenticado', 'fullName': 'Nome clínico privado', 'session_version': 42})
        self.assertEqual(response.status_code, 422)
        event = self.event()
        self.assertEqual(event[10], 'forbidden_fields')
        self.assertNotIn('Nome clínico privado', repr(event))

    def test_app_antigo_e_campo_desconhecido_nao_geram_suspeita(self):
        response = self.client.post('/api/sync/profile', json={'id': 'uid_autenticado', 'fullName': 'Paciente', 'campoLegado': 'compatibilidade'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.event()[10:12], ('normal', 0))

    def test_template_sem_query_uid_ou_segredos_e_xff_ignorado(self):
        response = self.client.get('/api/items/segredo-no-caminho?token=segredo-na-query', headers={
            'x-forwarded-for': '198.51.100.1', 'cf-connecting-ip': '2001:0db8::1',
            'cf-ipcountry': 'BR', 'cf-region': 'Sao Paulo', 'x-app-version': '1.3.10',
            'authorization': 'Bearer segredo-no-header', 'x-request-id': 'forjado'})
        event = self.event()
        self.assertEqual(event[2], '2001:db8::1')
        self.assertEqual(event[4:8], ('BR', 'Sao Paulo', '/api/items/{item_id}', 'GET'))
        self.assertEqual(str(event[0]), response.headers['x-request-id'])
        self.assertNotIn('segredo', repr(event))
        self.assertNotIn('198.51.100.1', repr(event))

    def test_rota_desconhecida_redigida_e_nao_inventa_uid(self):
        self.client.get('/api/pessoa-secreta?uid=forjado')
        event = self.event()
        self.assertIsNone(event[3])
        self.assertEqual(event[6], '/rota-nao-identificada')
        self.assertEqual(event[10], 'not_found')

    def test_poll_admin_nao_alimenta_o_proprio_risco(self):
        self.client.get('/api/admin/security-activity')
        self.assertEqual(self.telemetry.queue.qsize(), 0)

    def test_contexto_nao_vaza_para_proxima_requisicao(self):
        self.client.post('/api/sync/profile', json={'id': 'outro', 'fullName': 'Paciente'})
        self.event()
        self.client.get('/api/items/normal')
        self.assertEqual(self.event()[10], 'normal')
        mark_signal('uid_mismatch')  # Fora de uma requisição é inofensivo.

    def signed_context(self, path='/api/items/{item_id}', **changes):
        payload = {'path': path, 'method': 'GET', 'ip': '203.0.113.5', 'exp': time.time()+45, **changes}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        signature = hmac.new(b'test-secret', encoded.encode(), hashlib.sha256).hexdigest()
        return encoded+'.'+signature

    def test_contexto_assinado_separa_visitante_do_render_sem_aceitar_uid(self):
        with patch.dict('os.environ', {'SERVICE_TOKEN': 'test-secret'}):
            self.client.get('/api/items/abc', headers={'cf-connecting-ip': '198.51.100.9',
                'x-security-context': self.signed_context('/api/items/abc', userId='forjado')})
        event = self.event()
        self.assertEqual(event[2], '203.0.113.5')
        self.assertIsNone(event[3])
        self.assertEqual(event[14:16], ('198.51.100.9', 'portal'))

    def test_contexto_forjado_expirado_ou_de_outra_rota_ignorado(self):
        with patch.dict('os.environ', {'SERVICE_TOKEN': 'test-secret'}):
            for header in ('forjado.assinatura', self.signed_context('/api/items/abc', exp=time.time()-1), self.signed_context('/api/outra-rota')):
                self.client.get('/api/items/abc', headers={'cf-connecting-ip': '198.51.100.9', 'x-security-context': header})
                event = self.event()
                self.assertEqual(event[2], '198.51.100.9')
                self.assertEqual(event[15], 'gateway')

    def test_classificacao_origem_cliente(self):
        self.client.get('/api/items/1', headers={'x-app-version': '25'})
        self.assertEqual(self.event()[15], 'android')

        self.client.get('/api/items/2', headers={'origin': 'https://bragasaude.online'})
        self.assertEqual(self.event()[15], 'site')

        @self.app.post('/api/whatsapp/webhook')
        async def wa():
            return {'ok': True}
        self.client.post('/api/whatsapp/webhook')
        self.assertEqual(self.event()[15], 'meta')


class SecurityActivityAuthorization(unittest.TestCase):
    headers = auth_tests.PortalAuthTest.headers

    def setUp(self):
        auth_tests.PortalAuthTest.setUp(self)
        self.limit = patch.object(admin, 'auth_limit', AsyncMock())
        self.limit.start()

    def tearDown(self):
        self.limit.stop()
        auth_tests.PortalAuthTest.tearDown(self)

    def test_role_insuficiente_e_identidade_ausente(self):
        self.conn.fetchval.return_value = 'viewer'
        for path in ('/api/admin/security-activity', '/api/admin/security-events'):
            self.assertEqual(self.client.get(path, headers=self.headers(path)).status_code, 403)
            self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer test-service-secret'}).status_code, 401)

    def test_query_limitada_e_ip_uid_invalidos(self):
        self.conn.fetchval.return_value = 'admin'
        path = '/api/admin/security-events'
        for query in ('?ip=unknown', '?userId=email@example.test', '?minutes=0', '?minutes=99999', '?limit=99999'):
            self.assertEqual(self.client.get(path+query, headers=self.headers(path)).status_code, 422)


class TelemetryPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_fila_limitada_e_perda_explicita(self):
        telemetry = SecurityTelemetry(lambda: None, capacity=1)
        telemetry.enqueue(('evento',))
        telemetry.enqueue(('evento2',))
        self.assertEqual(telemetry.health()['dropped'], 1)
        await telemetry.flush()
        self.assertEqual(telemetry.health()['dropped'], 2)
        self.assertEqual(telemetry.health()['queued'], 0)
        self.assertEqual(telemetry.health()['error'], 'Falha de persistência da telemetria.')

    async def test_persistencia_em_lote(self):
        conn = AsyncMock()
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        telemetry = SecurityTelemetry(lambda: pool)
        for _ in range(3):
            telemetry.enqueue(('evento',))
        await telemetry.flush()
        self.assertEqual(len(conn.executemany.call_args.args[1]), 3)
        self.assertEqual(telemetry.health()['persisted'], 3)
        self.assertIsNotNone(telemetry.health()['lastFlush'])
