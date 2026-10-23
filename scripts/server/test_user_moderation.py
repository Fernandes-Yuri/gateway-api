"""Contratos de moderação e tentativas negativas sem acesso à produção."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from starlette.requests import Request
import test_portal_auth_security as auth_tests
from security_defense import StrictRateLimiter, get_client_ip
import user_moderation
from routers import admin


class ModerationRoutes(unittest.TestCase):
    headers = auth_tests.PortalAuthTest.headers

    def setUp(self):
        auth_tests.PortalAuthTest.setUp(self)
        self.limit = patch.object(admin, 'auth_limit', AsyncMock())
        self.limit.start()

    def tearDown(self):
        self.limit.stop()
        auth_tests.PortalAuthTest.tearDown(self)
    def test_operador_sem_role_nao_enumera_conta(self):
        self.conn.fetchval.return_value = 'viewer'
        for uid in ('existe', 'nao_existe'):
            path = '/api/admin/user-moderation'
            response = self.client.get(path + '?userId=' + uid, headers=self.headers(path))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['detail'], 'Permissão administrativa necessária.')

    def test_uid_email_e_duracoes_invalidas(self):
        self.conn.fetchval.return_value = 'admin'
        path = '/api/admin/user-restrictions'
        for changes in ({'userId': 'nome sobrenome'}, {'userId': 'email@example.test'}, {'minutes': 0}, {'minutes': 99999}, {'reason': '   '}):
            body = {'userId': 'uid_teste', 'level': 'no-sync', 'reason': 'Abuso', **changes}
            response = self.client.post(path, json=body, headers=self.headers(path))
            self.assertEqual(response.status_code, 422, response.text)

    def test_restricao_incrementa_versao_e_grava_auditoria(self):
        self.conn.fetchval.side_effect = ['admin', 3]
        path = '/api/admin/user-restrictions'
        response = self.client.post(path, json={'userId': 'uid_teste', 'level': 'no-sync', 'reason': '<b>Abuso</b>', 'minutes': 60}, headers={**self.headers(path), 'cf-connecting-ip': '203.0.113.9', 'x-forwarded-for': '198.51.100.1'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['session_version'], 3)
        self.assertIn('session_version+1', self.conn.fetchval.call_args.args[0])
        audit = self.conn.execute.call_args.args
        self.assertEqual(audit[1:], ('admin@example.test', '203.0.113.9', 'uid_teste', None, 'no-sync', 'Abuso'))

    def test_ip_manual_atualiza_e_limpa_limiter_e_audita(self):
        self.conn.fetchval.return_value = 'admin'
        limiter = StrictRateLimiter()
        with patch.object(admin._state, 'rate_limiter', limiter):
            path = '/api/admin/blocked-ips'
            response = self.client.post(path, json={'ip': '203.0.113.5', 'reason': 'Abuso', 'minutes': 43200}, headers=self.headers(path))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(limiter.is_ip_blocked('203.0.113.5')[0])
            path += '/203.0.113.5'
            self.assertEqual(self.client.delete(path, headers=self.headers(path)).status_code, 200)
            self.assertFalse(limiter.is_ip_blocked('203.0.113.5')[0])
        self.assertTrue(any('moderation_audit_log' in call.args[0] for call in self.conn.execute.call_args_list))

    def test_exclusao_chama_fluxo_transacional_com_uid_e_operador(self):
        self.conn.fetchval.return_value = 'admin'
        path = '/api/admin/delete-user-data'
        with patch('services.account_deletion.delete_account', AsyncMock(return_value={'success': False, 'deleted': True, 'storageCleanupPending': True})) as delete:
            response = self.client.post(path, json={'userId': 'uid_teste'}, headers={**self.headers(path), 'cf-connecting-ip': '203.0.113.9'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['storageCleanupPending'])
            delete.assert_awaited_once_with(self.pool, 'uid_teste', actor='admin@example.test', ip='203.0.113.9')


class RestrictionEnforcement(unittest.IsolatedAsyncioTestCase):
    def request(self, method, path):
        return Request({'type': 'http', 'method': method, 'path': path, 'headers': []})

    async def check(self, level, method, path, status=None):
        conn = AsyncMock()
        conn.fetchval.return_value = None
        conn.fetchrow.return_value = {'level': level, 'reason': 'Abuso'} if level else None
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        with patch.object(user_moderation, '_pool_provider', lambda: pool):
            if status:
                with self.assertRaises(HTTPException) as raised:
                    await user_moderation.require_not_restricted('uid_teste', self.request(method, path))
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.detail, 'Abuso')
            else:
                await user_moderation.require_not_restricted('uid_teste', self.request(method, path))
        return conn

    async def test_niveis_e_excecoes(self):
        await self.check('suspended', 'POST', '/api/sync/profile', 401)
        await self.check('suspended', 'GET', '/api/app/latest')
        await self.check('suspended', 'POST', '/api/admin/session-check')
        await self.check('no-sync', 'POST', '/api/sync/profile', 403)
        await self.check('no-sync', 'GET', '/api/sync/profile/uid_teste')
        await self.check('read-only', 'DELETE', '/api/sync/profile/uid_teste', 403)
        await self.check('read-only', 'GET', '/api/sync/profile/uid_teste')

    async def test_expiracao_consultada_sem_cache(self):
        conn = await self.check(None, 'POST', '/api/sync/profile')
        self.assertIn('expires_at > NOW()', conn.fetchrow.call_args.args[0])

    async def test_sessao_anterior_ao_bloqueio_exige_novo_login(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        conn.fetchval.side_effect = lambda sql, *args: datetime.fromtimestamp(100, timezone.utc) if "session_revoked_at" in sql else None
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        with patch.object(user_moderation, '_pool_provider', lambda: pool):
            with self.assertRaises(HTTPException) as revoked:
                await user_moderation.require_not_restricted('uid_teste', self.request('GET', '/api/sync/profile'), auth_time=99)
            self.assertEqual(revoked.exception.status_code, 401)
            await user_moderation.require_not_restricted('uid_teste', self.request('GET', '/api/sync/profile'), auth_time=101)

    async def test_loopback_unknown_e_spoof(self):
        limiter = StrictRateLimiter()
        for ip in ('127.0.0.1', 'unknown'):
            await limiter.set_manual_block(ip, True)
            self.assertEqual(limiter.is_ip_blocked(ip), (False, 0))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [(b'x-forwarded-for', b'198.51.100.1')], 'client': ('127.0.0.1', 80)})
        self.assertEqual(get_client_ip(request), '127.0.0.1')
