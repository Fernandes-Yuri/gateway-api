"""Integração do consentimento e expurgo no PostgreSQL efêmero do CI."""
import os
import unittest
from unittest.mock import AsyncMock, patch

import test_wa_parity_postgres as wa_postgres
from services.wa_channel_privacy import (consent_gate, prepare_deletion, resolve_deletion,
    phone_hash, DELETED_TEXT, TERMS_VERSION, PRIVACY_VERSION)
from routers import whatsapp as wa
from test_whatsapp_bateria import envelope

PHONE = '5500000000000'


@unittest.skipUnless(os.getenv('TEST_POSTGRES_URL'), 'PostgreSQL efêmero somente no CI')
class ChannelPrivacyPostgres(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wa_postgres.WhatsAppPostgresTests.asyncSetUp
    cleanup_database = wa_postgres.WhatsAppPostgresTests.cleanup_database

    async def test_accept_delete_and_relink_requires_new_acceptance(self):
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM whatsapp_channel_consents')
            await conn.execute("UPDATE profiles SET whatsapp_phone=$1 WHERE id='actor'", PHONE)
            await conn.execute("INSERT INTO vital_signs(user_id,heart_rate,measured_at) VALUES('actor',99,NOW())")
            await conn.execute("INSERT INTO assistant_preferences(user_id,preferences) VALUES('actor','{\"nickname\":\"AppAlias\",\"cup_ml\":350}'::jsonb)")
            await conn.execute("INSERT INTO whatsapp_message_receipts(message_id,user_id,phone_hash) VALUES('owned','actor',$1)", phone_hash(PHONE))
            await conn.execute("INSERT INTO pending_confirmations(user_id,confirmation_code,metric_type,value_json,expires_at,source_channel,channel_actor_id,channel_phone_hash) VALUES('actor','CONF-PRIVACY','PESO','{\"peso_kg\":80}'::jsonb,NOW()+interval '5 minutes','whatsapp','actor',$1)", phone_hash(PHONE))
        first = await consent_gate(self.pool, PHONE, text='oi')
        self.assertTrue(first[0])
        accepted = await consent_gate(self.pool, PHONE, button_id=first[2][0]['id'])
        self.assertIn('Aceite registrado', accepted[1])
        self.assertFalse((await consent_gate(self.pool, PHONE, text='qual meu peso?'))[0])
        await prepare_deletion(self.pool, PHONE, 'actor')
        result = await resolve_deletion(self.pool, PHONE, text='sim')
        self.assertEqual(result['message'], DELETED_TEXT)
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval("SELECT whatsapp_phone FROM profiles WHERE id='actor'"))
            self.assertEqual(await conn.fetchval("SELECT heart_rate FROM vital_signs WHERE user_id='actor'"), 99)
            self.assertIn('AppAlias', await conn.fetchval("SELECT preferences::text FROM assistant_preferences WHERE user_id='actor'"))
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_message_receipts'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM pending_confirmations'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_channel_consents'), 0)
            self.assertTrue(await conn.fetchval('SELECT EXISTS(SELECT 1 FROM whatsapp_channel_cleanup)'))
        self.assertIsNone(await resolve_deletion(self.pool, PHONE, text='sim'))
        self.assertTrue((await consent_gate(self.pool, PHONE, text='oi'))[0])

    async def test_wrong_challenge_version_change_and_cancel(self):
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM whatsapp_channel_consents')
        first = await consent_gate(self.pool, PHONE)
        self.assertNotIn('Aceite registrado', (await consent_gate(self.pool, PHONE, button_id='wa_accept_wrong'))[1])
        await consent_gate(self.pool, PHONE, button_id=first[2][0]['id'])
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM whatsapp_channel_consents WHERE phone_hash=$1', phone_hash(PHONE))
            self.assertEqual((row['terms_version'], row['privacy_version']), (TERMS_VERSION, PRIVACY_VERSION))
            await conn.execute("UPDATE whatsapp_channel_consents SET terms_version='old'")
        self.assertTrue((await consent_gate(self.pool, PHONE, text='oi'))[0])
        await prepare_deletion(self.pool, PHONE)
        self.assertIn('Não apaguei', await resolve_deletion(self.pool, PHONE, text='não'))

    async def test_full_pipeline_says_terms_before_any_health_processing(self):
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM whatsapp_channel_consents')
        send = AsyncMock()
        with patch.object(wa._state, 'db_pool', self.pool), \
             patch.object(wa, 'send_whatsapp_text', send), \
             patch.object(wa, 'send_whatsapp_interactive', AsyncMock(return_value={'success': False})), \
             patch.object(wa, '_process_authorized_whatsapp_payload', AsyncMock()) as inner:
            await wa.process_incoming_whatsapp_payload(envelope([{'from': PHONE,'type':'text','text':{'body':'glicemia 95'}}]))
        inner.assert_not_awaited()
        self.assertIn('Termos de Uso', send.await_args.args[1])
