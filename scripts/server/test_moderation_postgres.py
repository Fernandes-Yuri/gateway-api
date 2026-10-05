"""Moderação com PostgreSQL real e migrações no banco efêmero do CI."""
import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
import test_privacy_postgres as privacy
import user_moderation
from routers import admin


@unittest.skipUnless(privacy.TEST_DSN, 'PostgreSQL efêmero disponível somente no CI')
class ModerationPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await privacy.PrivacyPostgres.asyncSetUp(self)
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE TABLE admin_users(id text PRIMARY KEY,email text UNIQUE,role text)")
            await conn.execute("INSERT INTO admin_users VALUES ('operador','admin@example.test','admin')")
            for number in (18, 28, 29, 35):
                migration = next((privacy.ROOT / 'migrations').glob(f'{number:03d}_*.sql'))
                await conn.execute(migration.read_text(encoding='utf-8'))
        self.env = patch.dict('os.environ', {'SERVICE_TOKEN': 'test-service-secret'})
        self.env.start()
        self.state = patch.object(admin._state, 'db_pool', self.pool)
        self.state.start()
        self.provider = patch.object(user_moderation, '_pool_provider', lambda: self.pool)
        self.provider.start()

    async def asyncTearDown(self):
        self.provider.stop()
        self.state.stop()
        self.env.stop()
        await privacy.PrivacyPostgres.asyncTearDown(self)

    def request(self, method, path):
        payload = base64.urlsafe_b64encode(json.dumps({'email': 'admin@example.test', 'path': path, 'exp': time.time() + 40}).encode()).decode().rstrip('=')
        signature = hmac.new(b'test-service-secret', payload.encode(), hashlib.sha256).hexdigest()
        return Request({'type': 'http', 'method': method, 'path': path, 'headers': [
            (b'x-admin-identity', f'{payload}.{signature}'.encode()), (b'cf-connecting-ip', b'203.0.113.9')]})

    async def test_restricao_expira_revoga_sessao_e_preserva_auditoria(self):
        body = admin.UserRestrictionCreate(userId='paciente', level='no-sync', reason='Teste de homologação', minutes=60)
        result = await admin.restrict_user(body, self.request('POST', '/api/admin/user-restrictions'))
        self.assertEqual(result['session_version'], 1)
        with self.assertRaises(HTTPException) as denied:
            await user_moderation.require_not_restricted('paciente', self.request('POST', '/api/sync/profile'))
        self.assertEqual(denied.exception.status_code, 403)
        await user_moderation.require_not_restricted('paciente', self.request('GET', '/api/sync/profile/paciente'), auth_time=int(time.time()) + 2)
        with self.assertRaises(HTTPException) as revoked:
            await user_moderation.require_not_restricted('paciente', self.request('GET', '/api/sync/profile/paciente'), auth_time=int(time.time()) - 60)
        self.assertEqual(revoked.exception.status_code, 401)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_restrictions SET expires_at=NOW()-INTERVAL '1 second' WHERE user_id='paciente'")
        await user_moderation.require_not_restricted('paciente', self.request('POST', '/api/sync/profile'), auth_time=int(time.time()) + 2)
        await admin.unrestrict_user('paciente', self.request('DELETE', '/api/admin/user-restrictions/paciente'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM user_restrictions"), 0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM moderation_audit_log WHERE target_uid='paciente'"), 2)

    async def test_falha_auditoria_reverte_restricao_e_versao(self):
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'falha de teste'; END $$")
            await conn.execute('CREATE TRIGGER reject_audit BEFORE INSERT ON moderation_audit_log FOR EACH ROW EXECUTE FUNCTION reject_audit()')
        with self.assertRaises(Exception):
            await admin.restrict_user(admin.UserRestrictionCreate(userId='paciente', level='suspended', reason='Teste'), self.request('POST', '/api/admin/user-restrictions'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT session_version FROM profiles WHERE id='paciente'"), 0)
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM user_restrictions'), 0)
