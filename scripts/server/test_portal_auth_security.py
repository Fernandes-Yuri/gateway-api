"""Regressões do portal sem importar o servidor completo nem acessar produção."""
import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import admin
from routers.catalog import ManualPriceUpdate
from pydantic import ValidationError
import portal_auth_security


class PortalAuthTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'SERVICE_TOKEN': 'test-service-secret'})
        self.env.start()
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.state = patch.object(admin._state, 'db_pool', self.pool)
        self.state.start()
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.state.stop()

    def headers(self, path, email='admin@example.test', exp=None):
        payload = {'path': path, 'email': email, 'exp': exp or admin.time.time() + 40}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        signature = hmac.new(b'test-service-secret', encoded.encode(), hashlib.sha256).hexdigest()
        return {'Authorization': 'Bearer test-service-secret', 'X-Admin-Identity': encoded + '.' + signature}

    def test_rfc6238_base32_sem_padding(self):
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        with patch.object(admin.time, 'time', return_value=59):
            self.assertTrue(admin._totp_verify(secret, '287082'))
            self.assertFalse(admin._totp_verify(secret, '000000'))
            self.assertFalse(admin._totp_verify(secret, '87082'))

    def test_status_exige_identidade_assinada(self):
        response = self.client.get('/api/admin/totp/status', headers={'Authorization': 'Bearer test-service-secret'})
        self.assertEqual(response.status_code, 401)
        self.conn.fetchrow.assert_not_awaited()

    def test_status_rejeita_escopo_expiracao_e_assinatura(self):
        path = '/api/admin/totp/status'
        for headers in [self.headers('/outra-rota'), self.headers(path, exp=1),
                        {**self.headers(path), 'X-Admin-Identity': 'forjado.assinatura'}]:
            self.assertEqual(self.client.get(path, headers=headers).status_code, 401)

    def test_status_array_texto_e_identidade(self):
        path = '/api/admin/totp/status'
        self.conn.fetchrow.return_value = {'totp_enabled': True, 'recovery_left': 8}
        response = self.client.get(path, headers=self.headers(path))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['recoveryLeft'], 8)
        sql, email = self.conn.fetchrow.call_args.args
        self.assertIn('cardinality', sql)
        self.assertEqual(email, 'admin@example.test')

    def test_confirmacao_errada_nao_ativa(self):
        path = '/api/admin/totp/confirm'
        self.conn.fetchrow.return_value = {'id': 'a', 'totp_enabled': False, 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}
        with patch.object(admin, 'auth_limit', new=AsyncMock()), patch.object(admin.time, 'time', return_value=59):
            response = self.client.post(path, headers=self.headers(path), json={'email': 'admin@example.test', 'totpCode': '000000'})
        self.assertEqual(response.status_code, 401)
        self.conn.execute.assert_not_awaited()

    def test_confirmacao_gera_hashes_uma_vez(self):
        path = '/api/admin/totp/confirm'
        self.conn.fetchrow.return_value = {'id': 'a', 'totp_enabled': False, 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}
        with patch.object(admin, 'auth_limit', new=AsyncMock()), patch.object(admin.time, 'time', return_value=59):
            response = self.client.post(path, headers=self.headers(path), json={'email': 'admin@example.test', 'totpCode': '287082'})
            self.assertEqual(response.status_code, 200)
            codes = response.json()['recoveryCodes']
            self.assertEqual(len(set(codes)), 8)
            hashes = self.conn.execute.call_args.args[2]
            self.assertTrue(all(h.startswith('$2') for h in hashes))
            self.assertFalse(set(codes) & set(hashes))
            self.conn.fetchrow.return_value['totp_enabled'] = True
            repeated = self.client.post(path, headers=self.headers(path), json={'email': 'admin@example.test', 'totpCode': '287082'})
            self.assertEqual(repeated.status_code, 409)

    def test_recuperacao_exige_token_servico(self):
        response = self.client.post('/api/admin/password-reset', json={'email': 'a@example.test', 'password': 'senha-segura', 'master_totp_code': '287082'})
        self.assertEqual(response.status_code, 401)
        self.conn.execute.assert_not_awaited()

    def test_recuperacao_codigo_errado_nao_muda_senha(self):
        with patch.object(admin, 'auth_limit', new=AsyncMock()), patch.dict('os.environ', {'ADMIN_TOTP_SECRET': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}), patch.object(portal_auth_security.time, 'time', return_value=59):
            result = self.client.post('/api/admin/password-reset', headers=self.headers('/api/admin/password-reset'), json={'email': 'a@example.test', 'password': 'senha-segura', 'master_totp_code': '000000'})
        self.assertEqual(result.status_code, 401)
        self.conn.execute.assert_not_awaited()

    def test_recuperacao_revoga_sessoes_preserva_totp_e_hash(self):
        self.conn.fetchval.return_value = 1
        with patch.object(admin, 'auth_limit', new=AsyncMock()), patch.dict('os.environ', {'ADMIN_TOTP_SECRET': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}), patch.object(portal_auth_security.time, 'time', return_value=59):
            result = self.client.post('/api/admin/password-reset', headers=self.headers('/api/admin/password-reset'), json={'email': 'A@example.test', 'password': 'senha-segura', 'master_totp_code': '287082'})
        self.assertEqual(result.status_code, 200)
        sql, email, hashed = self.conn.execute.call_args.args
        self.assertIn('session_version = session_version + 1', sql)
        self.assertNotIn('totp_enabled', sql)
        self.assertEqual(email, 'a@example.test')
        import bcrypt
        self.assertTrue(bcrypt.checkpw(b'senha-segura', hashed.encode()))
        self.assertNotIn('senha-segura', result.text)
        self.conn.transaction.assert_called_once()

    def test_recuperacao_replay_nao_atualiza(self):
        self.conn.fetchval.return_value = None
        with patch.object(admin, 'auth_limit', new=AsyncMock()), patch.dict('os.environ', {'ADMIN_TOTP_SECRET': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}), patch.object(portal_auth_security.time, 'time', return_value=59):
            result = self.client.post('/api/admin/password-reset', headers=self.headers('/api/admin/password-reset'), json={'email': 'a@example.test', 'password': 'senha-segura', 'master_totp_code': '287082'})
        self.assertEqual(result.status_code, 401)
        self.assertFalse(any('UPDATE admin_users' in call.args[0] for call in self.conn.execute.call_args_list))

    def test_sessao_anterior_revogada(self):
        path = '/api/admin/session-check'
        self.conn.fetchval.return_value = 2
        for version, valid in [(1, False), (2, True)]:
            result = self.client.post(path, headers=self.headers(path), json={'email': 'admin@example.test', 'version': version})
            self.assertEqual(result.json()['valid'], valid)

    def test_desafio_valida_resolucao_frescor_e_uso_unico(self):
        path = '/api/admin/challenge/verify'
        payload = {'id': 'a' * 32, 'binding': 'b' * 64, 'purpose': 'reset', 'answer': 100}
        # O submit so passa com a linha marcada resolvida pelo check e fresca.
        for row, expected in [({'solved': True, 'fresh': True}, True),
                              ({'solved': False, 'fresh': True}, False),
                              ({'solved': True, 'fresh': False}, False), (None, False)]:
            self.conn.fetchrow.return_value = row
            result = self.client.post(path, headers=self.headers(path), json=payload)
            self.assertEqual(result.json()['success'], expected)
        sql, challenge_id, binding, purpose = self.conn.fetchrow.call_args.args
        self.assertIn('DELETE FROM', sql)
        self.assertEqual((challenge_id, binding, purpose), ('a' * 32, 'b' * 64, 'reset'))

    def test_preco_rejeita_negativo_infinito_excesso_e_centavos(self):
        for price in [-1, 0, float('inf'), float('nan'), 100000000, 1.234]:
            with self.assertRaises(ValidationError):
                ManualPriceUpdate(price=price)
        self.assertEqual(ManualPriceUpdate(price=8.50).price, 8.50)

    def test_cadastro_e_recuperacao_usam_mesmo_totp(self):
        with patch.dict('os.environ', {'ADMIN_TOTP_SECRET': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}), patch.object(portal_auth_security.time, 'time', return_value=59):
            step, fingerprint = portal_auth_security.master_step('287082')
            self.assertEqual(step, 1)
            self.assertEqual(len(fingerprint), 64)


if __name__ == '__main__':
    unittest.main()
