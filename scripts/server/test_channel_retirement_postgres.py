"""Migração de retirada remove só o canal antigo em PostgreSQL efêmero."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg

DSN = os.getenv('TEST_POSTGRES_URL')
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(DSN, 'PostgreSQL efêmero disponível no CI')
class RetirementMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(DSN)
        if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path != '/braga_privacy_test':
            raise RuntimeError('Somente banco efêmero braga_privacy_test')
        self.conn = await asyncpg.connect(DSN)
        self.schema = 'retirement_' + uuid4().hex
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.conn.execute(f'SET search_path TO "{self.schema}", public')
        # Estruturas mínimas incluem FKs para verificar preservação dos registros
        # compartilhados. O ensaio de migrations do CI cobre a cadeia integral.
        await self.conn.execute('''
            CREATE TABLE profiles(id text PRIMARY KEY, full_name text,
                whatsapp_phone text, whatsapp_linked_at timestamptz, totp_secret text);
            CREATE TABLE pending_confirmations(id int PRIMARY KEY, user_id text REFERENCES profiles,
                source_channel text);
            CREATE TABLE confirmation_logs(id int PRIMARY KEY, channel text,
                pending_conf_id int REFERENCES pending_confirmations);
            CREATE TABLE agent_actions(id int PRIMARY KEY, channel text);
            CREATE TABLE medication_dose_batches(id int PRIMARY KEY, channel text);
            CREATE TABLE medication_dose_batch_items(id int PRIMARY KEY,
                batch_id int REFERENCES medication_dose_batches ON DELETE CASCADE);
            CREATE TABLE vital_signs(id int PRIMARY KEY, confirmed_via text);
            CREATE TABLE medication_logs(id int PRIMARY KEY, channel text);
            INSERT INTO profiles VALUES ('patient','Teste','synthetic',now(),'synthetic');
            INSERT INTO pending_confirmations VALUES (1,'patient','whatsapp'),(2,'patient','app');
            INSERT INTO confirmation_logs VALUES (1,'whatsapp',1),(2,'app',2),(3,'app',1);
            INSERT INTO agent_actions VALUES (1,'whatsapp'),(2,'app');
            INSERT INTO medication_dose_batches VALUES (1,'whatsapp'),(2,'app');
            INSERT INTO medication_dose_batch_items VALUES (1,1),(2,2);
            INSERT INTO vital_signs VALUES (1,'whatsapp'),(2,'app');
            INSERT INTO medication_logs VALUES (1,'whatsapp'),(2,'app');
        ''')
        for name in ('whatsapp_channel_cleanup','whatsapp_channel_deletions',
                     'whatsapp_channel_consents','whatsapp_operation_keys',
                     'whatsapp_message_receipts','whatsapp_link_attempts'):
            await self.conn.execute(f'CREATE TABLE {name}(id int); INSERT INTO {name} VALUES(1)')

    async def asyncTearDown(self):
        await self.conn.execute('SET search_path TO public')
        await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.close()

    async def test_purge_is_scoped_and_keeps_clinical_history(self):
        async with self.conn.transaction():
            await self.conn.execute((ROOT/'migrations/066_retire_health_whatsapp.sql').read_text())
        for table in ('profiles','vital_signs','medication_logs'):
            self.assertEqual(await self.conn.fetchval(f'SELECT count(*) FROM {table}'),
                             1 if table == 'profiles' else 2)
        for table in ('pending_confirmations','agent_actions','medication_dose_batches',
                      'medication_dose_batch_items'):
            self.assertEqual(await self.conn.fetchval(f'SELECT id FROM {table}'), 2)
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM confirmation_logs'), 2)
        self.assertIsNone(await self.conn.fetchval('SELECT pending_conf_id FROM confirmation_logs WHERE id=3'))
        columns = await self.conn.fetch('SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2', self.schema, 'profiles')
        self.assertEqual({r['column_name'] for r in columns}, {'id','full_name'})
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema=$1 AND table_name LIKE 'whatsapp_%'", self.schema), 0)
