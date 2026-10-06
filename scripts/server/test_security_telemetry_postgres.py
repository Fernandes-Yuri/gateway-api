"""Risco por IP/UID com banco real, decaimento e tráfego legítimo."""
from datetime import datetime, timedelta, timezone
import unittest
import uuid
import test_moderation_postgres as moderation
import test_privacy_postgres as privacy
from routers import admin
from security_telemetry import SecurityTelemetry


@unittest.skipUnless(privacy.TEST_DSN, 'PostgreSQL efêmero disponível somente no CI')
class SecurityTelemetryPostgres(unittest.IsolatedAsyncioTestCase):
    request = moderation.ModerationPostgres.request

    async def asyncSetUp(self):
        await moderation.ModerationPostgres.asyncSetUp(self)
        async with self.pool.acquire() as conn:
            migration = next((privacy.ROOT / 'migrations').glob('036_*.sql'))
            await conn.execute(migration.read_text(encoding='utf-8'))

    async def asyncTearDown(self):
        await moderation.ModerationPostgres.asyncTearDown(self)

    async def seed(self, *, count, ip, uid, signal, weight, age=0):
        telemetry = SecurityTelemetry(lambda: self.pool)
        for _ in range(count):
            telemetry.enqueue((uuid.uuid4(), datetime.now(timezone.utc)-timedelta(minutes=age),
                ip, uid, 'BR', 'Sao Paulo', '/api/sync/profile', 'POST',
                403 if weight else 200, 10, signal, weight, '1.3.10', None, ip, 'gateway'))
        while not telemetry.queue.empty():
            await telemetry.flush()
        self.assertEqual(telemetry.dropped, 0)

    async def activity(self, group):
        return await admin.security_activity(self.request('GET', '/api/admin/security-activity'), groupBy=group, minutes=60, limit=100)

    async def test_risco_explicavel_uid_ip_e_decaimento(self):
        await self.seed(count=10, ip='203.0.113.9', uid='paciente', signal='uid_mismatch', weight=8)
        ip = (await self.activity('ip'))['data'][0]
        self.assertEqual(ip['level'], 'high')
        self.assertIn('UID diverge da identidade autenticada', ip['reasons'])
        self.assertEqual(ip['uids'], ['paciente'])
        self.assertEqual((await self.activity('uid'))['data'][0]['target'], 'paciente')
        events = await admin.security_events(self.request('GET', '/api/admin/security-events'), ip='203.0.113.9', userId=None, minutes=60, limit=100)
        self.assertEqual(len(events['data']), 10)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE security_request_events SET occurred_at=NOW()-INTERVAL '16 minutes'")
        self.assertEqual((await self.activity('ip'))['data'][0]['level'], 'normal')

    async def test_sincronizacao_legitima_ip_compartilhado_e_erro_isolado(self):
        await self.seed(count=120, ip='203.0.113.9', uid='paciente', signal='normal', weight=0)
        await self.seed(count=120, ip='203.0.113.9', uid='cuidador', signal='normal', weight=0)
        self.assertEqual((await self.activity('ip'))['data'], [])
        await self.seed(count=1, ip='203.0.113.9', uid='paciente', signal='invalid_payload', weight=2)
        row = (await self.activity('ip'))['data'][0]
        self.assertEqual(row['level'], 'normal')
        self.assertEqual(row['requests'], 241)
        self.assertEqual(set(row['uids']), {'paciente', 'cuidador'})
