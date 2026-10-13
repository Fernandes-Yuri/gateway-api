"""Publicação e downloads com banco descartável; nenhuma promoção em produção."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi import FastAPI
import httpx

from release_control import start_release_control
from release_publisher import ReleaseManifest, SIGNING_CERT_SHA256, publish_release
from routers import app as app_router

DSN = os.getenv('TEST_POSTGRES_URL')


def manifest(code=32, content=b'approved-synthetic-apk'):
    return ReleaseManifest(package_name='br.com.bragasaude', variant='release', debuggable=False,
        version_name=f'1.7.{code}', version_code=code, min_android_sdk=26,
        file_size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
        certificate_sha256=SIGNING_CERT_SHA256, source_sha='a' * 40, run_id=123)


@unittest.skipUnless(DSN, 'PostgreSQL descartável disponível no CI')
class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(DSN)
        if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path != '/braga_privacy_test':
            raise RuntimeError('Somente banco descartável braga_privacy_test')
        self.schema = 'apk_' + uuid.uuid4().hex
        self.control = await asyncpg.connect(DSN)
        self.pool = None
        self.addAsyncCleanup(self.cleanup_db)
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')
        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}"')
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute('''CREATE TABLE app_releases(id SERIAL PRIMARY KEY, version_name TEXT,
                version_code INTEGER UNIQUE, release_type TEXT, filename TEXT, file_size_bytes BIGINT,
                sha256_hash TEXT, download_url TEXT, changelog TEXT, min_android_sdk INTEGER,
                is_active BOOLEAN, created_at TIMESTAMPTZ DEFAULT now())''')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def cleanup_db(self):
        if self.pool:
            await self.pool.close()
        await self.control.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        await self.control.close()

    def stage(self, code=32, content=b'approved-synthetic-apk'):
        value = manifest(code, content)
        (self.root / value.filename).write_bytes(content)
        return value

    async def publish(self, value):
        return await publish_release(self.pool, self.root, value, 'https://api.fixture')

    @asynccontextmanager
    async def api(self):
        app = FastAPI(); app.include_router(app_router.router)
        with patch.object(app_router._state, 'db_pool', self.pool), patch.object(app_router, 'RELEASES_DIR', str(self.root)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://api.fixture') as client:
                yield client

    async def test_metadata_and_download_remain_bound_across_promotion(self):
        old = self.stage(31, b'old-apk'); await self.publish(old)
        async with self.api() as client:
            snapshot = (await client.get('/api/app/latest')).json()
            new = self.stage(32, b'new-apk'); await self.publish(new)
            previous = await client.get(snapshot['download_url'])
            latest = await client.get('/api/app/download')
        self.assertEqual(previous.content, b'old-apk')
        self.assertEqual(latest.content, b'new-apk')
        self.assertEqual(hashlib.sha256(previous.content).hexdigest(), snapshot['sha256_hash'].lower())

    async def test_database_failure_preserves_old_active_file_and_retry_succeeds(self):
        old = self.stage(31, b'old-apk'); await self.publish(old)
        value = self.stage(32, b'new-apk')
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE FUNCTION fail_new() RETURNS trigger AS $$ BEGIN
                RAISE EXCEPTION 'synthetic publication failure'; END; $$ LANGUAGE plpgsql;
                CREATE TRIGGER fail_new BEFORE INSERT ON app_releases FOR EACH ROW EXECUTE FUNCTION fail_new();""")
        with self.assertRaises(asyncpg.PostgresError):
            await self.publish(value)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT version_code FROM app_releases WHERE is_active'), 31)
            await conn.execute('DROP TRIGGER fail_new ON app_releases')
        self.assertEqual((self.root / old.filename).read_bytes(), b'old-apk')
        self.assertEqual((await self.publish(value))['status'], 'published')

    async def test_hash_failure_symlink_and_version_collision_do_not_change_active(self):
        old = self.stage(31); await self.publish(old)
        value = self.stage(32); (self.root / value.filename).write_bytes(b'corrupted')
        with self.assertRaises(ValueError): await self.publish(value)
        (self.root / value.filename).unlink()
        (self.root / value.filename).symlink_to(self.root / old.filename)
        with self.assertRaises(ValueError): await self.publish(value)
        collision = self.stage(31, b'other-bytes')
        with self.assertRaises(ValueError): await self.publish(collision)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM app_releases WHERE is_active'), 1)
            self.assertEqual(await conn.fetchval('SELECT sha256_hash FROM app_releases WHERE is_active'), old.sha256.upper())

    async def test_concurrency_and_repetition_have_one_active_release(self):
        value = self.stage(32)
        results = await asyncio.gather(*(self.publish(value) for _ in range(2)))
        self.assertEqual(sorted(r['status'] for r in results), ['already_active', 'published'])
        next_value = self.stage(33, b'newer')
        await self.publish(next_value)
        with self.assertRaises(ValueError): await self.publish(value)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM app_releases WHERE is_active'), 1)
            self.assertEqual(await conn.fetchval('SELECT version_code FROM app_releases WHERE is_active'), 33)

    async def test_corrupted_file_or_traversal_is_not_served(self):
        value = self.stage(); await self.publish(value)
        path = self.root / value.filename; path.chmod(0o600); path.write_bytes(b'bad')
        async with self.api() as client:
            self.assertEqual((await client.get('/api/app/download')).status_code, 503)
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE app_releases SET filename='../private.apk'")
            self.assertEqual((await client.get('/api/app/download')).status_code, 503)

    async def test_private_control_probes_publishes_and_notifies_only_once(self):
        value = self.stage()
        notify = AsyncMock()
        server, path = await start_release_control(self.root, lambda: self.pool, 'https://api.fixture', notify)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        async def call(payload):
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(json.dumps(payload).encode() + b'\n'); await writer.drain()
            result = json.loads(await reader.readline())
            writer.close(); await writer.wait_closed()
            return result
        try:
            self.assertEqual((await call({'action': 'probe'}))['status'], 'ready')
            self.assertEqual((await call(value.model_dump()))['status'], 'published')
            self.assertEqual((await call(value.model_dump()))['status'], 'already_active')
            self.assertEqual((await call({'variant': 'debug'}))['status'], 'error')
            if os.getuid() != 0:
                other_uid = os.getuid() + 1
                with patch('release_control.os.getuid', return_value=other_uid):
                    denied = await call(value.model_dump())
                self.assertEqual(denied['error'], 'PermissionError')
            notify.assert_awaited_once_with(value)
        finally:
            server.close(); await server.wait_closed(); path.unlink()


class ManifestTests(unittest.TestCase):
    def test_debug_other_package_certificate_or_path_like_version_rejected(self):
        for changes in ({'debuggable': True}, {'variant': 'debug'}, {'package_name': 'other'},
                        {'certificate_sha256': '0' * 64}, {'version_name': '../bad'}, {'version_code': 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ReleaseManifest.model_validate({**manifest().model_dump(), **changes})


class ControlPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_symlink_control_path_never_replaced_or_used(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'unrelated'; target.write_text('preserved')
            (root / 'publish.sock').symlink_to(target)
            with self.assertRaises(RuntimeError):
                await start_release_control(root, lambda: None, 'https://fixture')
            self.assertEqual(target.read_text(), 'preserved')
