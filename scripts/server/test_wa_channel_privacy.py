"""Barreira legal e exclusão do canal em dados sintéticos, sem contato Meta."""
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.wa_channel_privacy import (consent_gate, prepare_deletion, resolve_deletion, phone_hash,
    CONSENT_TEXT, DELETE_TEXT, DELETED_TEXT, TERMS_VERSION, PRIVACY_VERSION)
from services.wa_channel_storage import purge_channel_files, event_metadata
from test_wa_parity_regressions import connection, Pool
from test_whatsapp_bateria import envelope
from routers import whatsapp as wa

PHONE, OTHER = '5500000000000', '5500000000001'


def consent_row(accepted=False):
    now = datetime.now(timezone.utc)
    return {'challenge': uuid.uuid4(), 'terms_version': TERMS_VERSION,
            'privacy_version': PRIVACY_VERSION, 'presented_at': now - timedelta(seconds=2),
            'accepted_at': now - timedelta(seconds=1) if accepted else None,
            'expires_at': now + timedelta(minutes=10)}


class ConsentTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_message_and_bare_yes_do_not_accept(self):
        conn = connection()
        conn.fetchrow.return_value = None
        blocked, message, buttons = await consent_gate(Pool(conn), PHONE, text='glicemia 95')
        self.assertTrue(blocked)
        self.assertEqual(message, CONSENT_TEXT)
        self.assertEqual(len(buttons), 2)
        self.assertNotIn(PHONE, str(conn.execute.await_args))
        conn.fetchrow.return_value = consent_row()
        self.assertEqual((await consent_gate(Pool(conn), PHONE, text='sim'))[1], CONSENT_TEXT)

    async def test_explicit_acceptance_and_version_change(self):
        conn = connection()
        row = consent_row()
        conn.fetchrow.return_value = row
        blocked, message, buttons = await consent_gate(Pool(conn), PHONE, button_id='wa_accept_' + str(row['challenge']))
        self.assertTrue(blocked)  # Does not replay discarded clinical request.
        self.assertIn('Aceite registrado', message)
        self.assertEqual(buttons, [])
        self.assertIn('accepted_at=NOW()', conn.execute.await_args.args[0])
        conn.fetchrow.return_value = consent_row(True)
        self.assertFalse((await consent_gate(Pool(conn), PHONE, text='qual minha pressão?'))[0])
        conn.fetchrow.return_value['terms_version'] = 'old'
        self.assertTrue((await consent_gate(Pool(conn), PHONE, text='oi'))[0])
        self.assertIn('INSERT INTO whatsapp_channel_consents', conn.execute.await_args.args[0])

    async def test_wrong_nonce_and_preconsent_replays_cannot_accept(self):
        conn = connection()
        row = consent_row()
        conn.fetchrow.return_value = row
        self.assertEqual((await consent_gate(Pool(conn), OTHER, button_id='wa_accept_' + str(uuid.uuid4())))[1], CONSENT_TEXT)
        old = str(int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()))
        conn.fetchrow.return_value = consent_row(True)
        self.assertEqual(await consent_gate(Pool(conn), PHONE, text='glicemia 95', timestamp=old), (True, '', []))
        conn.fetchrow.return_value = row
        self.assertEqual((await consent_gate(Pool(conn), PHONE, text='Li e aceito', timestamp=old))[1], CONSENT_TEXT)

    async def test_acceptance_with_second_precision_timestamp_is_not_discarded(self):
        conn = connection()
        row = consent_row()
        row['presented_at'] = datetime.now(timezone.utc)
        conn.fetchrow.return_value = row
        message = (await consent_gate(Pool(conn), PHONE, text='Li e aceito',
            timestamp=str(int(row['presented_at'].timestamp()))))[1]
        self.assertIn('Aceite registrado', message)

    async def test_decline_and_missing_database_never_authorize(self):
        conn = connection()
        conn.fetchrow.return_value = consent_row()
        self.assertIn('bloqueado', (await consent_gate(Pool(conn), PHONE, text='não aceito'))[1])
        self.assertIn('DELETE FROM whatsapp_channel_consents', conn.execute.await_args.args[0])
        self.assertTrue((await consent_gate(None, PHONE))[0])


class DeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_expiry_and_unrelated_text_do_not_delete_data(self):
        conn = connection()
        message, buttons = await prepare_deletion(Pool(conn), PHONE, 'actor')
        self.assertEqual(message, DELETE_TEXT)
        self.assertEqual([b['title'] for b in buttons], ['SIM', 'NÃO'])
        conn.fetchrow.return_value = {'actor_id': 'actor', 'challenge': uuid.uuid4(),
                                    'expires_at': datetime.now(timezone.utc) + timedelta(minutes=5)}
        conn.execute.reset_mock()
        self.assertIsNone(await resolve_deletion(Pool(conn), PHONE, text='glicemia 95'))
        self.assertEqual(conn.execute.await_count, 1)  # lock only
        self.assertIn('Não apaguei', await resolve_deletion(Pool(conn), PHONE, text='não'))
        self.assertNotIn('UPDATE profiles', '\n'.join(call.args[0] for call in conn.execute.await_args_list))
        conn.fetchrow.return_value = None
        self.assertIn('expirou', await resolve_deletion(Pool(conn), PHONE, button_id='wa_delete_yes_' + str(uuid.uuid4())))

    async def test_deletion_preserves_app_and_enqueues_cleanup(self):
        conn = connection()
        conn.fetchrow.side_effect = [{'actor_id': 'actor', 'challenge': uuid.uuid4(),
                                    'expires_at': datetime.now(timezone.utc) + timedelta(minutes=5)}, {'id': 'actor'}]
        result = await resolve_deletion(Pool(conn), PHONE, text='sim')
        self.assertEqual(result['message'], DELETED_TEXT)
        sql = '\n'.join(call.args[0] for call in conn.execute.await_args_list)
        self.assertIn('whatsapp_phone=NULL', sql)
        self.assertIn('INSERT INTO whatsapp_channel_cleanup', sql)
        for table in ('profiles', 'vital_signs', 'biometry', 'medications', 'assistant_preferences'):
            self.assertNotIn('DELETE FROM ' + table + ' ', sql)
        self.assertLess(sql.index('DELETE FROM confirmation_logs'), sql.index('DELETE FROM pending_confirmations'))
        self.assertLess(sql.index('DELETE FROM assistant_preference_pending'), sql.index('DELETE FROM pending_confirmations'))

    async def test_changed_binding_and_old_yes_do_not_erase(self):
        conn = connection()
        row = {'actor_id': 'actor', 'challenge': uuid.uuid4(), 'expires_at': datetime.now(timezone.utc) + timedelta(minutes=5)}
        conn.fetchrow.side_effect = [row, None]
        self.assertIn('vínculo mudou', await resolve_deletion(Pool(conn), PHONE, text='sim'))
        self.assertNotIn('UPDATE profiles', '\n'.join(call.args[0] for call in conn.execute.await_args_list))
        conn.fetchrow.side_effect = None
        conn.fetchrow.return_value = row
        old = str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))
        self.assertIsNone(await resolve_deletion(Pool(conn), PHONE, text='sim', timestamp=old))


class StorageTests(unittest.TestCase):
    def test_metadata_and_cleanup_preserve_app_and_other_number(self):
        raw = envelope([{'id': 'synthetic', 'from': PHONE, 'type': 'text', 'text': {'body': 'sensitive-body'}},
                        {'id': 'other', 'from': OTHER, 'type': 'text', 'text': {'body': 'other-body'}}])
        metadata = event_metadata(raw)
        self.assertNotIn('sensitive-body', json.dumps(metadata))
        self.assertNotIn(PHONE, json.dumps(metadata))
        with tempfile.TemporaryDirectory() as directory:
            outbox, trace, dataset = [Path(directory) / name for name in ('outbox', 'trace', 'dataset')]
            outbox.write_text(json.dumps({'payload': raw}) + '\n')
            trace.write_text(json.dumps({'channel': 'whatsapp', 'fields': {'phone_hash': phone_hash(PHONE), 'text': 'private'}}) + '\n' +
                             json.dumps({'channel': 'chat-rest', 'fields': {'text': 'app'}}) + '\n')
            dataset.write_text(json.dumps({'source': 'whatsapp', 'messages': ['old']}) + '\n' +
                               json.dumps({'source': 'app_chat', 'messages': ['app']}) + '\n')
            purge_channel_files(PHONE, outbox_path=outbox, trace_path=trace, dataset_path=dataset)
            self.assertNotIn(phone_hash(PHONE), outbox.read_text())
            self.assertIn(phone_hash(OTHER), outbox.read_text())
            self.assertNotIn('private', trace.read_text())
            self.assertIn('app', trace.read_text())
            self.assertNotIn('whatsapp', dataset.read_text())
            self.assertIn('app_chat', dataset.read_text())

    def test_malformed_archive_cannot_be_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events'
            path.write_text('malformed-record\n')
            with self.assertRaises(ValueError):
                purge_channel_files(PHONE, outbox_path=path, trace_path=Path(directory)/'missing', dataset_path=Path(directory)/'missing2')
            self.assertEqual(path.read_text(), 'malformed-record\n')


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = connection()
        self.conn.fetchval.return_value = False
        self.conn.fetchrow.return_value = None
        self.pool, self.send = Pool(self.conn), AsyncMock()
        for item in (patch.object(wa._state, 'db_pool', self.pool),
                     patch.object(wa, 'send_whatsapp_text', self.send),
                     patch.object(wa, 'send_whatsapp_interactive', AsyncMock(return_value={'success': False})),
                     patch.object(wa, 'mark_whatsapp_message_read', AsyncMock())):
            item.start()
            self.addCleanup(item.stop)

    async def test_unaccepted_text_audio_and_callback_never_reach_executors(self):
        with patch.object(wa, '_process_authorized_whatsapp_payload', AsyncMock()) as inner, \
             patch.object(wa, 'download_whatsapp_media', AsyncMock()) as download, \
             patch.object(wa, '_handle_authorized_confirmation_callback', AsyncMock()) as handler:
            for message in ({'from': PHONE, 'type': 'text', 'text': {'body': 'glicemia 95'}},
                            {'from': PHONE, 'type': 'audio', 'audio': {'id': 'synthetic'}}):
                await wa.process_incoming_whatsapp_payload(envelope([message]))
            await wa.handle_confirmation_callback('confirm_SYNTHETIC', PHONE)
        self.assertEqual(self.send.await_count, 3)
        inner.assert_not_awaited()
        download.assert_not_awaited()
        handler.assert_not_awaited()
        self.assertIn('Termos de Uso', self.send.await_args.args[1])

    async def test_emergency_stays_available_without_acceptance_or_database(self):
        with patch.object(wa._state, 'db_pool', None), \
             patch.object(wa, 'send_whatsapp_emergency', AsyncMock()) as emergency:
            await wa.process_incoming_whatsapp_payload(envelope([{'from': PHONE, 'type': 'text', 'text': {'body': 'dor no peito'}}]))
        emergency.assert_awaited_once()
        self.conn.fetchrow.assert_not_awaited()

    async def test_link_request_is_blocked_before_consent(self):
        with patch.object(wa, 'handle_link_attempt', AsyncMock()) as link:
            await wa.process_incoming_whatsapp_payload(envelope([{'from': PHONE, 'type': 'text', 'text': {'body': '123456'}}]))
        link.assert_not_awaited()

    async def test_acceptance_button_is_consumed_before_health_callback(self):
        row = consent_row()
        async def fetchrow(sql, *args):
            return row if 'whatsapp_channel_consents' in sql else None
        self.conn.fetchrow.side_effect = fetchrow
        with patch.object(wa, '_handle_authorized_confirmation_callback', AsyncMock()) as handler:
            await wa.handle_confirmation_callback('wa_accept_' + str(row['challenge']), PHONE)
        handler.assert_not_awaited()
        self.assertIn('Aceite registrado', self.send.await_args.args[1])

    async def test_cleanup_failure_does_not_claim_completed_deletion(self):
        with patch.object(wa, 'resolve_deletion', AsyncMock(return_value={
                'deleted': True, 'actor_id': 'actor', 'message': DELETED_TEXT})), \
             patch('services.wa_channel_storage.finish_channel_cleanup', AsyncMock(side_effect=OSError)):
            await wa._channel_privacy_guard(PHONE, text='sim')
        self.assertIn('ainda está em andamento', self.send.await_args.args[1])
        self.assertNotIn('Apaguei os dados ativos', self.send.await_args.args[1])
