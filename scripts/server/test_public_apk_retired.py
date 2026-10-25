"""O gateway nunca oferece bytes, links ou notificações do canal APK aposentado."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import app as app_router


class PublicApkRetired(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(app_router.router)
        self.client = TestClient(app)

    def assert_retired(self, response):
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(data['status'], 'coming_soon')
        self.assertEqual(data['distribution_channel'], 'google_play')
        self.assertIs(data['available'], False)
        for field in ('download_url', 'filename', 'sha256_hash', 'version_code', 'store_url'):
            self.assertNotIn(field, data)
        self.assertNotIn('application/vnd.android.package-archive', response.headers['content-type'])

    def test_old_links_and_version_queries_are_gone_without_querying_catalog(self):
        pool = MagicMock()
        pool.acquire.side_effect = AssertionError('Retired routes must not read catalog')
        for state in (None, pool):
            with patch.object(app_router._state, 'db_pool', state):
                for path in ('/api/app/latest', '/api/app/version', '/api/app/download',
                             '/api/app/download?version_code=39', '/api/app/download?token=old-link'):
                    self.assert_retired(self.client.get(path))
        pool.acquire.assert_not_called()

    def test_old_broadcast_payload_cannot_send_update_or_restore_download_link(self):
        messaging = MagicMock()
        with patch.object(app_router._state, 'messaging', messaging):
            self.assert_retired(self.client.post('/api/app/notify-update', json={
                'version_name': '1.9.2', 'version_code': 39,
                'download_url': 'https://example.test/old.apk'
            }))
            self.assert_retired(self.client.post('/api/app/notify-update'))
        self.assertEqual(messaging.mock_calls, [])

    def test_legacy_landing_file_does_not_advertise_an_apk(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'index.html').write_text('<a href="/api/app/download">Baixar APK</a>')
            with patch.dict('os.environ', {'ONBOARDING_DIR': root}):
                response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['app_available'])
        self.assertNotIn('Baixar APK', response.text)


class InternalReleaseNotificationRetired(unittest.IsolatedAsyncioTestCase):
    async def test_archive_callback_does_not_broadcast_updates(self):
        self.assertEqual(await app_router.notify_published_release(object()),
                         {'status': 'retired', 'sent_count': 0, 'failed_count': 0})
