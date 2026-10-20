"""Contratos editoriais e fronteira público/privado, sem acessar produção."""
import base64
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from routers import admin, editorial


def article():
    return dict(title='Como buscar informação em saúde mental', slug='informacao-saude-mental',
                description='Informação geral com fontes verificáveis para famílias e pessoas que buscam cuidado.',
                category='Saúde mental', body='Um parágrafo informativo. ' * 12,
                author_name='Equipe editorial', author_bio='Equipe responsável pela informação editorial.',
                sources=[{'title': 'Ministério da Saúde', 'url': 'https://www.gov.br/saude/'}], reviewed=True)


class EditorialTest(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.patches = [patch.object(admin._state, 'db_pool', self.pool),
                        patch.dict('os.environ', {'SERVICE_TOKEN': 'test-editorial-service'}),
                        patch.object(editorial, 'auth_limit', AsyncMock())]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(editorial.router)
        self.client = TestClient(app)
        self.id = uuid4()
        now = datetime.now(timezone.utc)
        self.row = dict(id=self.id, owner_email='writer@example.test', slug=article()['slug'],
                        draft=json.dumps(article()), published=json.dumps(article()), version=2,
                        state='published', published_at=now, updated_at=now, created_at=now,
                        published_modified_at=now)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def headers(self, path):
        payload = base64.urlsafe_b64encode(json.dumps({'path': path, 'email': 'writer@example.test',
                                                     'exp': time.time() + 40}).encode()).decode().rstrip('=')
        signature = hmac.new(b'test-editorial-service', payload.encode(), hashlib.sha256).hexdigest()
        return {'Authorization': 'Bearer test-editorial-service', 'X-Admin-Identity': payload + '.' + signature}

    def test_private_requires_service_and_signed_identity(self):
        path = '/api/admin/editorial/articles'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer test-editorial-service'}).status_code, 401)
        self.conn.fetch.assert_not_awaited()

    def test_admin_without_editorial_permission_cannot_read_or_publish(self):
        self.conn.fetchrow.return_value = dict(admin_role='admin', role=None, enabled=None)
        path = '/api/admin/editorial/articles'
        self.assertEqual(self.client.get(path, headers=self.headers(path)).status_code, 403)
        path += '/' + str(self.id)
        self.assertEqual(self.client.post(path, headers=self.headers(path), json={'version': 2, 'action': 'publish'}).status_code, 403)

    def test_writer_cannot_publish(self):
        self.conn.fetchrow.return_value = dict(admin_role='admin', role='writer', enabled=True)
        path = '/api/admin/editorial/articles/' + str(self.id)
        res = self.client.post(path, headers=self.headers(path), json={'version': 2, 'action': 'publish'})
        self.assertEqual(res.status_code, 403)

    def test_stale_version_does_not_overwrite(self):
        self.conn.fetchrow.side_effect = [dict(admin_role='admin', role='writer', enabled=True), self.row]
        path = '/api/admin/editorial/articles/' + str(self.id)
        response = self.client.put(path, headers=self.headers(path), json={'version': 1, 'article': article()})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conn.execute.await_count, 1)  # Apenas lock, nenhuma escrita.

    def test_public_projection_never_contains_drafts_or_access_email(self):
        self.conn.fetchval.return_value = 1
        self.conn.fetch.return_value = [self.row]
        response = self.client.get('/api/editorial/articles')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('writer@example.test', response.text)
        self.assertNotIn('draft', response.text)
        self.assertIn("state='published'", self.conn.fetch.call_args.args[0])

    def test_unpublished_slug_returns_404(self):
        self.conn.fetchrow.return_value = None
        self.assertEqual(self.client.get('/api/editorial/articles/draft-only').status_code, 404)

    def test_publish_requires_sources_and_review(self):
        data = article()
        data['sources'] = []
        row = {**self.row, 'draft': json.dumps(data)}
        self.conn.fetchrow.side_effect = [dict(admin_role='admin', role='publisher', enabled=True), row]
        path = '/api/admin/editorial/articles/' + str(self.id)
        response = self.client.post(path, headers=self.headers(path), json={'version': 2, 'action': 'publish'})
        self.assertEqual(response.status_code, 422)
        self.conn.execute.assert_not_awaited()

    def test_schema_rejects_script_image_category_and_injected_fields(self):
        for changes in [{'cover_url': 'javascript:alert(1)'}, {'category': 'inexistente'}, {'owner_email': 'other@example.test'}, {'slug': '../../private'}]:
            with self.assertRaises(ValidationError):
                editorial.Article.model_validate({**article(), **changes})
