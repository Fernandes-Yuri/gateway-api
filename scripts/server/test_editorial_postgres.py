"""Integração editorial em schema descartável de PostgreSQL de testes."""
import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from routers import admin, editorial
from test_editorial import article


@unittest.skipUnless(os.getenv('TEST_POSTGRES_URL'), 'PostgreSQL de testes não configurado')
class EditorialPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema = 'test_editorial_' + uuid4().hex
        self.root = await asyncpg.connect(os.environ['TEST_POSTGRES_URL'])
        await self.root.execute(f'CREATE SCHEMA {self.schema}')
        self.pool = await asyncpg.create_pool(os.environ['TEST_POSTGRES_URL'], min_size=1, max_size=3,
                                             server_settings={'search_path': self.schema})
        async with self.pool.acquire() as conn:
            await conn.execute('CREATE TABLE admin_users(email TEXT PRIMARY KEY,role TEXT NOT NULL)')
            await conn.execute("INSERT INTO admin_users VALUES('publisher@example.test','superadmin'),('writer@example.test','admin')")
            sql = Path(__file__).resolve().parents[2] / 'migrations/067_clinicas_editorial.sql'
            await conn.execute(sql.read_text())
            await conn.execute(sql.read_text())  # Idempotência.
            await conn.execute("INSERT INTO editorial_members(email,role) VALUES('writer@example.test','writer')")
        self.previous_pool = admin._state.db_pool
        admin._state.db_pool = self.pool
        self.env = patch.dict('os.environ', {'SERVICE_TOKEN': 'test-editorial-service'})
        self.env.start()
        self.rate = patch.object(editorial, 'auth_limit', AsyncMock())
        self.rate.start()
        app = FastAPI()
        app.include_router(editorial.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test.invalid')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.rate.stop()
        self.env.stop()
        admin._state.db_pool = self.previous_pool
        await self.pool.close()
        await self.root.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.root.close()

    def headers(self, path, email='publisher@example.test'):
        payload = base64.urlsafe_b64encode(json.dumps({'path': path, 'email': email, 'exp': time.time() + 40}).encode()).decode().rstrip('=')
        signature = hmac.new(b'test-editorial-service', payload.encode(), hashlib.sha256).hexdigest()
        return {'Authorization': 'Bearer test-editorial-service', 'X-Admin-Identity': payload + '.' + signature}

    async def test_full_lifecycle_preserves_snapshot_and_revokes_public_content(self):
        path = '/api/admin/editorial/articles/' + str(uuid4())
        data = article()
        response = await self.client.put(path, headers=self.headers(path), json={'version': 0, 'article': data})
        self.assertEqual(response.status_code, 200, response.text)
        version = response.json()['version']
        public = '/api/editorial/articles/' + data['slug']
        self.assertEqual((await self.client.get(public)).status_code, 404)
        response = await self.client.post(path, headers=self.headers(path), json={'version': version, 'action': 'publish'})
        self.assertEqual(response.status_code, 200, response.text)
        version = response.json()['version']
        published = (await self.client.get(public)).json()
        self.assertEqual(published['title'], data['title'])
        self.assertNotIn('owner_email', published)
        draft = {**data, 'title': 'Uma nova edição ainda não publicada'}
        response = await self.client.put(path, headers=self.headers(path), json={'version': version, 'article': draft})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get(public)).json()['title'], data['title'])
        self.assertEqual((await self.client.post(path, headers=self.headers(path), json={'version': version, 'action': 'publish'})).status_code, 409)
        version = response.json()['version']
        response = await self.client.post(path, headers=self.headers(path), json={'version': version, 'action': 'archive'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get(public)).status_code, 404)
        self.assertEqual((await self.client.get('/api/editorial/articles')).json()['items'], [])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM editorial_events'), 4)

    async def test_writer_cannot_edit_other_author_or_publish(self):
        path = '/api/admin/editorial/articles/' + str(uuid4())
        response = await self.client.put(path, headers=self.headers(path), json={'version': 0, 'article': article()})
        version = response.json()['version']
        headers = self.headers(path, 'writer@example.test')
        self.assertEqual((await self.client.put(path, headers=headers, json={'version': version, 'article': article()})).status_code, 404)
        self.assertEqual((await self.client.post(path, headers=headers, json={'version': version, 'action': 'publish'})).status_code, 403)
