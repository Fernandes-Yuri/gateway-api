"""Fila, controle concorrente e API reais num PostgreSQL efêmero local/CI."""
import asyncio
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import asyncpg
from urllib.parse import urlparse
from routers import whatsapp_internal as api
from routers import whatsapp_intake as webhook
from schemas.whatsapp_intake import ControlRequest, TextRequest, ModelReply
from services import wa_intake_store as store, wa_intake_worker as worker
from services.wa_intake_privacy import consent_gate, prepare_deletion, resolve_deletion

PHONE = '5500000000000'
RECEIVER = 'synthetic-receiver'


def envelope(message=None, statuses=None):
    value = {'metadata': {'phone_number_id': RECEIVER}}
    if message: value['messages'] = [message]
    if statuses: value['statuses'] = statuses
    return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'value': value}]}]}


@unittest.skipUnless(os.getenv('TEST_POSTGRES_URL'), 'Requer PostgreSQL efêmero')
class IntakePostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn=os.environ['TEST_POSTGRES_URL']
        parsed=urlparse(dsn)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/braga_privacy_test':
            raise RuntimeError('Somente PostgreSQL efêmero braga_privacy_test')
        self.schema='intake_'+uuid.uuid4().hex
        self.control=await asyncpg.connect(dsn)
        self.pool=None
        self.addAsyncCleanup(self.cleanup_database)
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')
        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}",public')
        self.pool=await asyncpg.create_pool(dsn,min_size=1,max_size=4,setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE TABLE profiles(id TEXT PRIMARY KEY,full_name TEXT); INSERT INTO profiles VALUES('actor','Ana')")
            await conn.execute('CREATE TABLE admin_users(id TEXT PRIMARY KEY); CREATE TABLE schema_migrations(version TEXT PRIMARY KEY)')
            await conn.execute((Path(__file__).resolve().parents[2]/'migrations/028_admin_auth_security.sql').read_text())

    async def cleanup_database(self):
        if self.pool: await self.pool.close()
        await self.control.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        await self.control.close()

    async def setup_intake(self):
        env = patch.dict(os.environ, {'WHATSAPP_PHONE_NUMBER_ID':RECEIVER})
        env.start()
        self.addCleanup(env.stop)
        root = Path(__file__).resolve().parents[2]
        async with self.pool.acquire() as conn:
            await conn.execute((root / 'migrations/068_whatsapp_intake.sql').read_text())
            await conn.execute("INSERT INTO whatsapp_intake_operators(uid) VALUES('yuri')")
            await conn.execute("DELETE FROM whatsapp_intake_consents")
        self.previous_pool = api._state.db_pool
        api._state.db_pool = self.pool
        self.addCleanup(setattr, api._state, 'db_pool', self.previous_pool)

    async def ingest(self, text='oi', message_id=None, kind='text', **extra):
        mid = message_id or uuid.uuid4().hex
        message = {'id': mid, 'from': PHONE, 'type': kind, 'timestamp': str(int(__import__('time').time()))}
        if kind == 'text': message['text'] = {'body': text}
        message.update(extra)
        await store.ingest(self.pool, envelope(message), RECEIVER)
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('SELECT * FROM whatsapp_intake_conversations WHERE phone=$1', PHONE)

    async def accept(self):
        await consent_gate(self.pool, PHONE, text='oi')
        await consent_gate(self.pool, PHONE, text='Li e aceito')

    async def test_inbox_ack_dedup_and_no_preconsent_model_or_media(self):
        await self.setup_intake()
        await self.ingest('dados que não podem ir ao modelo', message_id='same')
        await self.ingest('dados que não podem ir ao modelo', message_id='same')
        with patch.object(worker, 'generate', AsyncMock()) as model, patch.object(worker.transport, 'download_media', AsyncMock()) as media:
            self.assertTrue(await worker.run_inbox_once(self.pool))
            self.assertFalse(await worker.run_inbox_once(self.pool))
        model.assert_not_awaited()
        media.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_inbox'), 1)
            self.assertEqual(await conn.fetchval('SELECT payload FROM whatsapp_intake_inbox'), '{}')
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM whatsapp_intake_messages WHERE origin='contact'"), 0)

    async def test_claim_during_generation_discards_bot_and_allows_human_idempotently(self):
        await self.setup_intake()
        await self.accept()
        row = await self.ingest('Meu nome é Alex Souza')
        started, proceed = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            started.set()
            await proceed.wait()
            return ModelReply(reply='É para você?', updates={})
        with patch.object(worker, 'generate', delayed):
            task = asyncio.create_task(worker.run_inbox_once(self.pool))
            await asyncio.wait_for(started.wait(), 5)
            result = await store.control(self.pool, row['id'], 'yuri', 'claim', ControlRequest(epoch=0, idempotency_key='claim-request'))
            proceed.set()
            await asyncio.wait_for(task, 5)
        self.assertEqual(result['mode'], 'human')
        request = TextRequest(epoch=1, idempotency_key='send-request', text='Oi, aqui é o Yuri.')
        first = await store.human_message(self.pool, row['id'], 'yuri', request)
        second = await store.human_message(self.pool, row['id'], 'yuri', request)
        self.assertEqual(first, second)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM whatsapp_intake_messages WHERE origin='bot'"), 0)
        with patch.object(worker.transport, 'send', AsyncMock(return_value='meta-human')) as send:
            await worker.run_send_once(self.pool)
        send.assert_awaited_once_with(PHONE, 'Oi, aqui é o Yuri.', content=None, mime=None)

    async def test_claim_waits_for_external_send_and_does_not_resume_old_bot(self):
        await self.setup_intake()
        row = await self.ingest()
        async with store.locked(self.pool, row['id']) as (conn, current):
            await conn.execute('UPDATE whatsapp_intake_conversations SET last_inbound_at=NOW() WHERE id=$1', row['id'])
            await store.queue_message(conn, current, 'resposta em voo')
        started, proceed = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await proceed.wait()
            return 'meta-bot'
        with patch.object(worker.transport, 'send', delayed):
            sending = asyncio.create_task(worker.run_send_once(self.pool))
            await asyncio.wait_for(started.wait(), 5)
            claiming = asyncio.create_task(store.control(self.pool, row['id'], 'yuri', 'claim', ControlRequest(epoch=0, idempotency_key='claim-send')))
            await asyncio.sleep(0.05)
            self.assertFalse(claiming.done())
            proceed.set()
            await asyncio.wait_for(asyncio.gather(sending, claiming), 5)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT mode FROM whatsapp_intake_conversations'), 'human')

    async def test_audio_still_transcribes_under_human_control_and_image_is_private(self):
        await self.setup_intake()
        await self.accept()
        row = await self.ingest(kind='audio', audio={'id': 'media-synthetic', 'mime_type': 'audio/ogg'})
        await store.control(self.pool, row['id'], 'yuri', 'claim', ControlRequest(epoch=0, idempotency_key='claim-audio'))
        with patch.object(worker.transport, 'download_media', AsyncMock(return_value=(b'private-audio', 'audio/ogg'))), \
             patch.object(worker.transport, 'validate_media', AsyncMock()), \
             patch.object(worker.transport, 'transcribe', AsyncMock(return_value='É para minha mãe.')), \
             patch.object(worker, 'generate', AsyncMock()) as model:
            await worker.run_inbox_once(self.pool)
        model.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT transcript FROM whatsapp_intake_messages WHERE origin='contact'"), 'É para minha mãe.')
            media_id = await conn.fetchval('SELECT id FROM whatsapp_intake_media')
        app = FastAPI()
        app.include_router(api.router)
        async def authenticated(): return await api.check_operator('ordinary-patient')
        app.dependency_overrides[api.operator] = authenticated
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.get(f'/api/internal/whatsapp/media/{media_id}')).status_code, 403)
            app.dependency_overrides[api.operator] = lambda: 'yuri'
            response = await client.get(f'/api/internal/whatsapp/media/{media_id}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'private-audio')
            partial = await client.get(f'/api/internal/whatsapp/media/{media_id}',headers={'Range':'bytes=0-6'})
            self.assertEqual(partial.status_code,206)
            self.assertEqual(partial.content,b'private')

    async def test_summary_must_be_sent_then_confirmed_and_notifies_once(self):
        await self.setup_intake()
        await self.accept()
        await self.ingest('Todos os dados sintéticos')
        fields = {k: {'value': None, 'status': 'declined', 'evidence': 'dados'} for k in worker.pending_fields({})}
        response = ModelReply(reply='Tudo certo.', updates=fields)
        with patch.object(worker, 'generate', AsyncMock(return_value=response)):
            await worker.run_inbox_once(self.pool)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT intake_status FROM whatsapp_intake_conversations'), 'awaiting_summary_confirmation')
        with patch.object(worker.transport, 'send', AsyncMock(return_value='meta-summary')):
            await worker.run_send_once(self.pool)
        await self.ingest('sim', message_id='confirmation')
        await worker.run_inbox_once(self.pool)
        await self.ingest('sim', message_id='confirmation')
        self.assertFalse(await worker.run_inbox_once(self.pool))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT mode FROM whatsapp_intake_conversations'), 'waiting_human')
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_handoffs'), 1)

    async def test_delete_cascades_media_queues_and_emits_tombstone(self):
        await self.setup_intake()
        await self.accept()
        row = await self.ingest('dados privados')
        with patch.object(worker, 'generate', AsyncMock(return_value=ModelReply(reply='Me conta mais.', updates={}))):
            await worker.run_inbox_once(self.pool)
        # Telefone sintético não está vinculado ao perfil baseline.
        await prepare_deletion(self.pool, PHONE)
        result = await resolve_deletion(self.pool, PHONE, text='sim')
        self.assertTrue(result['deleted'])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_conversations'), 0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_messages'), 0)
            self.assertEqual(await conn.fetchval('SELECT kind FROM whatsapp_intake_events'), 'conversation.deleted')
            self.assertEqual(await conn.fetchval("SELECT full_name FROM profiles WHERE id='actor'"), 'Ana')
        self.assertIsNotNone(row)

    async def test_webhook_commits_durable_inbox_and_rejects_wrong_number(self):
        await self.setup_intake()
        app = FastAPI()
        app.include_router(webhook.router)
        previous = webhook._state.db_pool
        webhook._state.db_pool = self.pool
        self.addCleanup(setattr, webhook._state, 'db_pool', previous)
        with patch.dict(os.environ, {'WHATSAPP_INTAKE_ENABLED': 'true'}), \
             patch.object(webhook, 'verify_meta_signature', return_value=True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                payload = envelope({'id': 'webhook-test', 'from': PHONE, 'type': 'text', 'text': {'body': 'oi'}})
                response = await client.post('/api/whatsapp/webhook', json=payload)
                self.assertEqual(response.status_code, 200)
                async with self.pool.acquire() as conn:
                    self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_inbox'), 1)
                payload['entry'][0]['changes'][0]['value']['metadata']['phone_number_id'] = 'wrong'
                self.assertEqual((await client.post('/api/whatsapp/webhook', json=payload)).status_code, 403)

    async def test_claim_unauthorized_stale_epoch_and_revoked_operator_cannot_send(self):
        await self.setup_intake()
        row = await self.ingest()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            await store.control(self.pool, row['id'], 'patient', 'claim', ControlRequest(epoch=0, idempotency_key='claim-denied'))
        request = ControlRequest(epoch=0, idempotency_key='claim-success')
        await store.control(self.pool, row['id'], 'yuri', 'claim', request)
        self.assertEqual((await store.control(self.pool, row['id'], 'yuri', 'claim', request))['epoch'], 1)
        with self.assertRaises(HTTPException):
            await store.control(self.pool, row['id'], 'yuri', 'release', ControlRequest(epoch=0, idempotency_key='release-stale'))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE whatsapp_intake_operators SET enabled=FALSE WHERE uid='yuri'")
        with self.assertRaises(HTTPException):
            await store.human_message(self.pool, row['id'], 'yuri', TextRequest(epoch=1,idempotency_key='send-revoked',text='não enviar'))

    async def test_recovery_does_not_replay_ambiguous_external_send(self):
        await self.setup_intake()
        await self.accept()
        await self.ingest('Quero uma consulta')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE whatsapp_intake_inbox SET status='processing',lease_until=NOW()-INTERVAL '1 minute',attempts=1")
        with patch.object(worker, 'generate', AsyncMock(return_value=ModelReply(reply='É para você?', updates={}))):
            await worker.run_inbox_once(self.pool)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE whatsapp_intake_messages SET delivery='sending',available_at=NOW()-INTERVAL '4 minutes' WHERE origin='bot'")
        await worker.maintenance(self.pool)
        with patch.object(worker.transport, 'send', AsyncMock()) as send:
            self.assertFalse(await worker.run_send_once(self.pool))
        send.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT delivery FROM whatsapp_intake_messages WHERE origin='bot'"), 'uncertain')

    async def test_http_contract_list_messages_controls_and_devices(self):
        await self.setup_intake()
        await self.accept()
        row = await self.ingest('Quero falar com um atendente')
        await worker.run_inbox_once(self.pool)
        app = FastAPI()
        app.include_router(api.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            base = '/api/internal/whatsapp'
            self.assertEqual((await client.get(base+'/conversations')).status_code,401)
            async def denied(): return await api.check_operator('patient')
            app.dependency_overrides[api.operator] = denied
            self.assertEqual((await client.get(base+'/events')).status_code,403)
            app.dependency_overrides[api.operator] = lambda: 'yuri'
            listed = await client.get(base+'/conversations')
            self.assertEqual(listed.status_code,200,listed.text)
            snapshot = listed.json()
            self.assertEqual(snapshot['items'][0]['mode'],'waiting_human')
            self.assertEqual(snapshot['items'][0]['handoff']['status'],'pending')
            detail = await client.get(base+f"/conversations/{row['id']}")
            self.assertEqual(detail.status_code,200,detail.text)
            history = await client.get(base+f"/conversations/{row['id']}/messages")
            self.assertEqual(history.status_code,200,history.text)
            self.assertEqual(history.json()['items'][0]['body'],'Quero falar com um atendente')
            event_page = await client.get(base+'/events')
            self.assertEqual(event_page.status_code,200,event_page.text)
            self.assertTrue(event_page.json()['items'])
            claim = await client.post(base+f"/conversations/{row['id']}/claim",json={'epoch':1,'idempotency_key':'http-claim'})
            self.assertEqual(claim.status_code,200,claim.text)
            sent = await client.post(base+f"/conversations/{row['id']}/messages",json={'epoch':2,'idempotency_key':'http-message','text':'Oi, aqui é o Yuri.'})
            self.assertEqual(sent.status_code,202,sent.text)
            wrong = await client.post(base+f"/conversations/{row['id']}/messages",json={'epoch':2,'idempotency_key':'http-message','text':'outro conteúdo'})
            self.assertEqual(wrong.status_code,409)
            device = str(uuid.uuid4())
            registered = await client.post(base+'/devices',json={'device_id':device,'token':'synthetic-device-token-never-used'})
            self.assertEqual(registered.status_code,204,registered.text)
            self.assertEqual((await client.delete(base+'/devices/'+device)).status_code,204)

    async def test_event_cursor_waits_for_commit_and_read_state_is_per_operator(self):
        await self.setup_intake()
        row = await self.ingest()
        async with self.pool.acquire() as first, self.pool.acquire() as second:
            first_tx, second_tx = first.transaction(), second.transaction()
            await first_tx.start()
            first_id = await store.event(first,row['id'],'first')
            await second_tx.start()
            pending = asyncio.create_task(store.event(second,row['id'],'second'))
            await asyncio.sleep(0.05)
            self.assertFalse(pending.done())
            await first_tx.commit()
            second_id = await asyncio.wait_for(pending,5)
            await second_tx.commit()
        self.assertGreater(second_id,first_id)
        page = await api.event_page(first_id)
        self.assertEqual(page['items'][0]['id'],second_id)

    async def test_exhausted_inbox_unblocks_following_messages_and_alerts_human(self):
        await self.setup_intake()
        row = await self.ingest()
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE whatsapp_intake_conversations SET consented=TRUE WHERE id=$1',row['id'])
            await conn.execute("UPDATE whatsapp_intake_inbox SET status='processing',attempts=5,lease_until=NOW()-INTERVAL '1 minute'")
        await worker.maintenance(self.pool)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM whatsapp_intake_inbox'),'failed')
            self.assertEqual(await conn.fetchval('SELECT mode FROM whatsapp_intake_conversations'),'waiting_human')
            self.assertEqual(await conn.fetchval('SELECT reason FROM whatsapp_intake_handoffs'),'worker_failed')

    async def test_absolute_expiry_blocks_reads_commands_and_cascades_media(self):
        await self.setup_intake()
        row = await self.ingest('conteúdo sintético')
        media_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            mid = await conn.fetchval("INSERT INTO whatsapp_intake_messages(conversation_id,origin,kind,body,epoch) VALUES($1,'contact','image','sintético',0) RETURNING id", row['id'])
            await conn.execute("INSERT INTO whatsapp_intake_media(id,message_id,mime_type,content,status) VALUES($1,$2,'image/png',$3,'ready')", media_id, mid, b'fixture')
            await conn.execute("UPDATE whatsapp_intake_conversations SET created_at=NOW()-INTERVAL '23 hours 58 minutes',updated_at=NOW() WHERE id=$1", row['id'])
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.operator] = lambda: 'yuri'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            base = '/api/internal/whatsapp'
            self.assertEqual((await client.get(f'{base}/media/{media_id}')).status_code,200)
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE whatsapp_intake_conversations SET created_at=NOW()-INTERVAL '23 hours 59 minutes 1 second',updated_at=NOW() WHERE id=$1", row['id'])
            # Nada depende da execução prévia do expurgo nem do updated_at recente.
            self.assertEqual((await client.get(f'{base}/conversations')).json()['items'],[])
            for endpoint in [f'/conversations/{row["id"]}',f'/conversations/{row["id"]}/messages',f'/media/{media_id}']:
                self.assertEqual((await client.get(base+endpoint)).status_code,404)
            response = await client.post(f'{base}/conversations/{row["id"]}/claim',json={'epoch':0,'idempotency_key':'expiry-test-claim'})
            self.assertEqual(response.status_code,410)
        # Configuração antiga não pode manter conteúdo sensível por 30 dias.
        with patch.dict(os.environ,{'WHATSAPP_INTAKE_RETENTION_DAYS':'30'}):
            await worker.maintenance(self.pool)
        async with self.pool.acquire() as conn:
            for table in ['conversations','messages','media','inbox']:
                self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_'+table),0)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM whatsapp_intake_events WHERE kind='conversation.deleted'"),1)

    async def test_retention_keeps_recent_conversation_and_expires_old_events(self):
        await self.setup_intake()
        row = await self.ingest('sintético recente')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE whatsapp_intake_conversations SET updated_at=NOW()-INTERVAL '60 days' WHERE id=$1",row['id'])
            await conn.execute("INSERT INTO whatsapp_intake_events(conversation_id,kind,created_at) VALUES($1,'old',NOW()-INTERVAL '24 hours')",row['id'])
        await worker.maintenance(self.pool)
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT EXISTS(SELECT 1 FROM whatsapp_intake_conversations WHERE id=$1)',row['id']))
            self.assertFalse(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM whatsapp_intake_events WHERE kind='old')"))

    async def test_migration_rollback_and_reapply(self):
        await self.setup_intake()
        root = Path(__file__).resolve().parents[2]
        async with self.pool.acquire() as conn:
            await conn.execute((root/'migrations/068_whatsapp_intake_rollback.sql').read_text())
            self.assertIsNone(await conn.fetchval("SELECT to_regclass('whatsapp_intake_conversations')"))
            await conn.execute((root/'migrations/068_whatsapp_intake.sql').read_text())
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_conversations'),0)

    async def test_enrollment_uses_portal_master_totp_and_consumes_shared_step(self):
        await self.setup_intake()
        import base64
        import hashlib
        import hmac
        import struct
        import time
        from schemas.whatsapp_intake import EnrollmentRequest,RefreshRequest
        from services import wa_intake_auth as auth
        from fastapi import HTTPException
        secret='JBSWY3DPEHPK3PXP'  # fixture pública e sintética
        step=int(time.time())//30
        raw=hmac.new(base64.b32decode(secret),struct.pack('>Q',step),hashlib.sha1).digest()
        offset=raw[-1]&15
        code=f'{(struct.unpack(">I",raw[offset:offset+4])[0]&0x7fffffff)%1000000:06d}'
        request=EnrollmentRequest(device_id=uuid.uuid4(),master_totp_code=code,display_name='Yuri teste')
        with patch.dict(os.environ,{'ADMIN_TOTP_SECRET':secret}):
            session=await auth.enroll(self.pool,request,'synthetic-ip')
            self.assertEqual(await auth.authenticate(self.pool,'Bearer '+session['access_token']),session['operator_uid'])
            with self.assertRaises(HTTPException):
                await auth.enroll(self.pool,request,'synthetic-ip')
            refreshed=await auth.refresh_session(self.pool,RefreshRequest(refresh_token=session['refresh_token']),'synthetic-ip')
            with self.assertRaises(HTTPException):
                await auth.authenticate(self.pool,'Bearer '+session['access_token'])
            with self.assertRaises(HTTPException):
                await auth.refresh_session(self.pool,RefreshRequest(refresh_token=session['refresh_token']),'synthetic-ip')
            await auth.logout(self.pool,session['operator_uid'])
            with self.assertRaises(HTTPException):
                await auth.authenticate(self.pool,'Bearer '+refreshed['access_token'])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM admin_master_uses'),1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM whatsapp_intake_sessions'),0)

    async def test_homologation_token_authenticates_without_master(self):
        await self.setup_intake()
        from services import wa_intake_auth as auth
        from schemas.whatsapp_intake import RefreshRequest
        uid = await auth.authenticate(self.pool, 'Bearer wha_homologacao_temporaria_12345678')
        self.assertEqual(uid, 'device:homologacao')
        refreshed = await auth.refresh_session(self.pool, RefreshRequest(refresh_token='whr_homologacao_temporaria'), 'synthetic-ip')
        self.assertEqual(refreshed['operator_uid'], 'device:homologacao')

    async def test_session_expiry_rotation_of_master_and_enrollment_http_contract(self):
        await self.setup_intake()
        from services import wa_intake_auth as auth
        from schemas.whatsapp_intake import EnrollmentRequest
        from fastapi import HTTPException
        with patch.dict(os.environ,{'ADMIN_TOTP_SECRET':'JBSWY3DPEHPK3PXP'}), \
             patch.object(auth,'master_step',return_value=(100,auth.fingerprint())):
            session=await auth.enroll(self.pool,EnrollmentRequest(device_id=uuid.uuid4(),master_totp_code='123456'),'synthetic-ip')
            app=FastAPI(); app.include_router(api.router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                headers={'Authorization':'Bearer '+session['access_token']}
                self.assertEqual((await client.get('/api/internal/whatsapp/conversations',headers=headers)).status_code,200)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE whatsapp_intake_sessions SET access_expires_at=NOW()-INTERVAL '1 minute'")
                self.assertEqual((await client.get('/api/internal/whatsapp/conversations',headers=headers)).status_code,401)
            with patch.dict(os.environ,{'ADMIN_TOTP_SECRET':'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}):
                with self.assertRaises(HTTPException):
                    await auth.authenticate(self.pool,'Bearer '+session['access_token'])

    async def test_webhook_get_signature_and_malformed_payload(self):
        await self.setup_intake()
        import hashlib
        import hmac
        app=FastAPI(); app.include_router(webhook.router)
        old=webhook._state.db_pool; webhook._state.db_pool=self.pool
        self.addCleanup(setattr,webhook._state,'db_pool',old)
        with patch.dict(os.environ,{'WHATSAPP_INTAKE_ENABLED':'true','WHATSAPP_APP_SECRET':'synthetic-fixture','WHATSAPP_VERIFY_TOKEN':'synthetic-verify'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                self.assertEqual((await client.get('/api/whatsapp/webhook',params={'hub.mode':'subscribe','hub.verify_token':'synthetic-verify','hub.challenge':'challenge'})).text,'challenge')
                self.assertEqual((await client.post('/api/whatsapp/webhook',content=b'{}')).status_code,401)
                raw=b'not-json'
                signature='sha256='+hmac.new(b'synthetic-fixture',raw,hashlib.sha256).hexdigest()
                self.assertEqual((await client.post('/api/whatsapp/webhook',content=raw,headers={'X-Hub-Signature-256':signature})).status_code,400)
