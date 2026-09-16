import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from api_identity import firebase_uid
from family_api import message_lifetime, serialize_message, install_family_routes, purge_expired_family_messages
from test_alignment import namespace


class LifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, 18, tzinfo=timezone.utc)

    def test_delayed_upload_keeps_original_expiry(self):
        sent, expires = message_lifetime('2026-09-16T10:00:00-03:00', self.now)
        self.assertEqual(sent, self.now - timedelta(hours=5))
        self.assertEqual(expires, self.now + timedelta(hours=19))

    def test_exact_expiry_and_old_replays_rejected(self):
        for age in [24, 25, 48]:
            with self.assertRaises(HTTPException) as error:
                message_lifetime((self.now-timedelta(hours=age)).isoformat(), self.now)
            self.assertEqual(error.exception.status_code, 410)

    def test_future_clock_and_missing_timezone_rejected(self):
        for value in ['invalid', '2026-09-16T18:00:00', '2026-09-17T18:00:00Z']:
            with self.assertRaises(HTTPException) as error:
                message_lifetime(value, self.now)
            self.assertEqual(error.exception.status_code, 422)

    def test_small_clock_skew_cannot_extend_retention(self):
        sent, expires = message_lifetime((self.now+timedelta(minutes=1)).isoformat(), self.now)
        self.assertEqual(sent, self.now)
        self.assertEqual(expires, self.now+timedelta(hours=24))

    def test_tombstone_never_serializes_message_content(self):
        result = serialize_message(dict(id=uuid.uuid4(), message_text='private', sender_name='name',
                                       icon_type='MED', deleted_at=self.now))
        self.assertEqual(result['message_text'], '')
        self.assertEqual(result['sender_name'], '')
        self.assertEqual(result['icon_type'], 'LOVE')


class RetentionRoutes(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        app = FastAPI()
        install_family_routes(app, lambda: self.pool, namespace['FamilyBindingSyncDto'], namespace['FamilyMessageSyncDto'])
        app.dependency_overrides[firebase_uid] = lambda: 'patient'
        self.client = TestClient(app)
        self.id = str(uuid.uuid4())

    def test_delete_is_idempotent_when_row_already_gone(self):
        self.conn.fetchrow.return_value = None
        self.assertEqual(200, self.client.post('/api/family/message/delete', json={'id':self.id}).status_code)

    def test_cannot_delete_another_senders_message(self):
        self.conn.fetchrow.return_value = dict(sender_id='other', id=uuid.UUID(self.id),patient_id='patient')
        self.assertEqual(403, self.client.post('/api/family/message/delete', json={'id':self.id}).status_code)
        self.conn.execute.assert_not_awaited()

    def test_delete_scrubs_content_and_retains_expiry(self):
        self.conn.fetchrow.return_value = dict(sender_id='patient', id=uuid.UUID(self.id),patient_id='patient')
        self.assertEqual(200, self.client.post('/api/family/message/delete', json={'id':self.id}).status_code)
        sql = self.conn.execute.call_args.args[0]
        self.assertIn("message_text=''", sql)
        self.assertNotIn('expires_at=', sql)

    def test_expired_message_is_rejected_before_insert(self):
        result = self.client.post('/api/sync/family-message', json=dict(id=self.id, patientId='patient', senderId='patient',
            senderName='Test', messageText='expired', iconType='LOVE', isRead=False,
            sentAt=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()))
        self.assertEqual(410, result.status_code)
        self.conn.fetchrow.assert_not_awaited()

    def test_pages_are_stable_bounded_and_filter_expiry(self):
        self.conn.fetch.return_value = []
        self.assertEqual(200, self.client.get('/api/family/messages/patient?limit=500&offset=200').status_code)
        sql, patient, limit, offset = self.conn.fetch.call_args.args
        self.assertEqual((patient,limit,offset), ('patient',200,200))
        self.assertIn('expires_at > now()',sql)
        self.assertIn('sent_at DESC, id DESC',sql)


class PurgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_removes_expired_tombstones_and_messages(self):
        conn = AsyncMock()
        conn.execute.return_value='DELETE 3'
        pool=MagicMock()
        pool.acquire.return_value.__aenter__=AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__=AsyncMock(return_value=False)
        self.assertEqual(3, await purge_expired_family_messages(pool))
        self.assertIn('expires_at <= now()', conn.execute.call_args.args[0])
