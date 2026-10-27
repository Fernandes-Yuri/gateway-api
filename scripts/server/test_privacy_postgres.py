"""Integração real no PostgreSQL efêmero do CI; nunca aceita DSN de produção."""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import asyncpg
from fastapi import HTTPException
from routers import sync
from services import account_deletion

ROOT = Path(__file__).resolve().parents[2]
TEST_DSN = os.getenv("TEST_POSTGRES_URL")


@unittest.skipUnless(TEST_DSN, "PostgreSQL efêmero disponível somente no CI")
class PrivacyPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(TEST_DSN)
        if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("O teste aceita apenas o banco local braga_privacy_test")
        self.schema = "privacy_" + uuid.uuid4().hex
        self.control = await asyncpg.connect(TEST_DSN)
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')

        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}", public')

        self.pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / "scripts/docker/init_schema.sql").read_text(encoding="utf-8"))
            for number in (10, 17, 18, 31, 32, 33):
                migration = next((ROOT / "migrations").glob(f"{number:03d}_*.sql"))
                await conn.execute(migration.read_text(encoding="utf-8"))
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)")
            await conn.execute((ROOT / "migrations/056_firebase_account_reconciliation.sql").read_text(encoding="utf-8"))
            await conn.execute((ROOT / "migrations/071_account_pause_and_mail.sql").read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO profiles(id,full_name) VALUES ('paciente','Paciente'),('cuidador','Cuidador')")

    async def test_social_reaction_change_removal_and_private_audience(self):
        from datetime import datetime, timezone
        async with self.pool.acquire() as conn:
            await conn.execute("ALTER TABLE social_posts ADD COLUMN visibility TEXT NOT NULL DEFAULT 'PUBLIC'")
            public = await conn.fetchval("INSERT INTO social_posts(user_id,post_type,title) VALUES ('paciente','milestone','Teste') RETURNING id")
            private = await conn.fetchval("INSERT INTO social_posts(user_id,post_type,title,visibility) VALUES ('paciente','milestone','Privado','FAMILY') RETURNING id")
        state = SimpleNamespace(db_pool=self.pool)
        payload = dict(postId=public, userId="cuidador", createdAt=datetime.now(timezone.utc).isoformat())
        with patch.object(sync, "_state", state):
            for kind in ("apoio", "palmas", "palmas"):
                await sync.sync_post_reaction(sync.PostReactionSyncDto(**payload, reactionType=kind), uid="cuidador")
            async with self.pool.acquire() as conn:
                self.assertEqual(1, await conn.fetchval("SELECT COUNT(*) FROM post_reactions WHERE post_id=$1", public))
                self.assertEqual("palmas", await conn.fetchval("SELECT reaction_type FROM post_reactions WHERE post_id=$1", public))
                await conn.execute("INSERT INTO post_reactions(post_id,user_id) VALUES ($1,'paciente')", public)
            await sync.delete_post_reaction(public, uid="cuidador")
            await sync.delete_post_reaction(public, uid="cuidador")
            async with self.pool.acquire() as conn:
                self.assertEqual(["paciente"], [r["user_id"] for r in await conn.fetch("SELECT user_id FROM post_reactions WHERE post_id=$1", public)])
            with self.assertRaises(HTTPException) as caught:
                await sync.sync_post_reaction(sync.PostReactionSyncDto(**{**payload, "postId": private}), uid="cuidador")
            self.assertEqual(404, caught.exception.status_code)
            async with self.pool.acquire() as conn:
                await conn.execute("INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,caregiver_relation,connection_code,status,expires_at) VALUES ('paciente','cuidador','Teste','Familiar','reaction-test','ACTIVE',NOW()+INTERVAL '1 day')")
            await sync.sync_post_reaction(sync.PostReactionSyncDto(**{**payload, "postId": private}), uid="cuidador")

    async def test_exam_storage_removal_preserves_patient_and_medications(self):
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)")
            for number in (0, 8, 13, 16, 30):
                migration = next((ROOT / "migrations").glob(f"{number:03d}_*.sql"))
                await conn.execute(migration.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO medications(user_id,name,schedule_time) VALUES ('paciente','Teste','08:00')")
            sql = (ROOT / "migrations/044_remove_exam_storage.sql").read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute(sql)
            for table in ("exams", "exam_items", "exam_cloud_consents", "clinical_references"):
                self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", table))
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles"), 2)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM medications"), 1)

    async def asyncTearDown(self):
        await self.pool.close()
        await self.control.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.control.close()

    async def test_caregiver_deletion_preserves_patient_history_and_patient_deletion_expires_files(self):
        async with self.pool.acquire() as conn:
            med = await conn.fetchval("INSERT INTO medications(user_id,name,schedule_time) VALUES ('paciente','Medicamento de teste','08:00') RETURNING id")
            await conn.execute("INSERT INTO prescriptions(medication_id,patient_id,issued_on,validity_days,prescriber_name,prescriber_crm,confirmed_by) VALUES ($1,'paciente',CURRENT_DATE,30,'Teste','123','cuidador')",med)
            await conn.execute("INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type) VALUES ('paciente','cuidador','Cuidador','TESTE')")
            await conn.execute("INSERT INTO medication_logs(user_id,medication_id,taken_at,actor_id) VALUES ('paciente',$1,NOW(),'cuidador')",med)
            await conn.execute("INSERT INTO symptoms_diary(patient_id,reported_by,reported_at,symptoms_text,input_method) VALUES ('paciente','cuidador',NOW(),'Teste','TEXT')")
            await conn.execute("INSERT INTO medication_stock_adjustments(medication_id,patient_id,actor_id,new_total_units,idempotency_key) VALUES ($1,'paciente','cuidador',30,'teste')",med)
            await conn.execute("INSERT INTO prescription_files(filename,patient_id) VALUES ('anexo.pdf','paciente')")
        with tempfile.TemporaryDirectory() as root, patch.object(account_deletion,"STORAGE_DIR",root):
            result = await account_deletion.delete_account(self.pool,"cuidador",actor="operador de teste")
            self.assertTrue(result["success"])
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM medications"),1)
                self.assertIsNone(await conn.fetchval("SELECT confirmed_by FROM prescriptions"))
                self.assertIsNone(await conn.fetchval("SELECT actor_id FROM care_audit_trail"))
                self.assertIsNone(await conn.fetchval("SELECT actor_id FROM medication_logs"))
                self.assertIsNone(await conn.fetchval("SELECT actor_id FROM medication_stock_adjustments"))
                self.assertIsNone(await conn.fetchval("SELECT reported_by FROM symptoms_diary"))
            path = Path(root,"prescriptions","anexo.pdf")
            path.parent.mkdir()
            path.write_bytes(b"anexo de teste")
            self.assertTrue((await account_deletion.delete_account(self.pool,"paciente",actor="titular"))["success"])
            self.assertFalse(path.exists())
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM prescriptions"),0)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM prescription_files"),0)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM user_deletion_logs"),2)

    async def test_push_cleanup_is_scoped_and_retry_removes_orphans_without_duplicate_audit(self):
        async with self.pool.acquire() as conn:
            for i in range(5):
                await conn.execute("INSERT INTO device_push_tokens(token,user_id) VALUES ($1,'paciente')", f"patient-{i}")
            await conn.execute("INSERT INTO device_push_tokens(token,user_id) VALUES ('other','cuidador')")
        with patch.object(account_deletion, "cleanup_account_files", AsyncMock(return_value=True)):
            self.assertTrue((await account_deletion.delete_account(self.pool, "paciente", actor="titular"))["success"])
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM device_push_tokens WHERE user_id='paciente'"), 0)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM device_push_tokens WHERE user_id='cuidador'"), 1)
                await conn.execute("INSERT INTO device_push_tokens(token,user_id) VALUES ('legacy','paciente')")
            self.assertTrue((await account_deletion.delete_account(self.pool, "paciente", actor="titular"))["success"])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM device_push_tokens WHERE user_id='paciente'"), 0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM user_deletion_logs WHERE user_id='paciente'"), 1)

    async def test_deletion_removes_posts_and_cross_account_reactions_but_preserves_other_posts(self):
        async with self.pool.acquire() as conn:
            patient_post = await conn.fetchval("INSERT INTO social_posts(user_id,post_type,title) VALUES ('paciente','milestone','Teste') RETURNING id")
            other_post = await conn.fetchval("INSERT INTO social_posts(user_id,post_type,title) VALUES ('cuidador','milestone','Preservado') RETURNING id")
            await conn.execute("INSERT INTO post_reactions(post_id,user_id) VALUES ($1,'cuidador'),($2,'paciente')", patient_post, other_post)
        with patch.object(account_deletion, "cleanup_account_files", AsyncMock(return_value=True)):
            await account_deletion.delete_account(self.pool, "paciente", actor="titular")
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM social_posts WHERE user_id='paciente'"), 0)
            self.assertEqual(await conn.fetchval("SELECT id FROM social_posts WHERE user_id='cuidador'"), other_post)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM post_reactions"), 0)

    async def test_failed_transaction_rolls_back_every_deletion_and_cleanup_job(self):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO feedbacks(user_id,category,message) VALUES ('paciente','bug','Teste')")
            await conn.execute("INSERT INTO device_push_tokens(token,user_id) VALUES ('rollback-token','paciente')")
            await conn.execute("CREATE FUNCTION reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'falha de teste'; END $$")
            await conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON profiles FOR EACH ROW EXECUTE FUNCTION reject_delete()")
        with self.assertRaises(asyncpg.PostgresError):
            await account_deletion.delete_account(self.pool,"paciente",actor="titular")
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM feedbacks"),1)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM device_push_tokens WHERE user_id='paciente'"),1)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_storage_cleanup"),0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM user_deletion_logs"),0)

    async def test_feedback_retry_and_legacy_backfill_respect_ownership(self):
        fid = str(uuid.uuid4())
        with patch.object(sync._state,"db_pool",self.pool), patch.object(sync._state,"rate_limiter",MagicMock(check=AsyncMock())), patch.object(sync,"get_client_ip",return_value="unknown"):
            feedback = sync.FeedbackSyncDto(id=fid,userId="paciente",category="bug",message="Teste")
            for _ in range(2):
                self.assertEqual((await sync.sync_feedback(feedback,MagicMock(),uid="paciente"))["id"],fid)
            with self.assertRaises(HTTPException) as error:
                await sync.sync_feedback(feedback.model_copy(update={"userId":"cuidador"}),MagicMock(),uid="cuidador")
            self.assertEqual(error.exception.status_code,409)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM feedbacks"),1)
            for patient,filename in (("paciente","legado.pdf"),("paciente","ambiguo.pdf"),("cuidador","ambiguo.pdf")):
                await conn.execute("INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details) VALUES ($1,$1,'Teste','PRESCRIPTION_ANALYZED',$2::jsonb)",patient,json.dumps({"file_url":f"https://exemplo.test/api/files/prescriptions/{filename}"}))
            await conn.execute((ROOT / "migrations/034_prescription_files_legacy.sql").read_text(encoding="utf-8"))
            self.assertEqual(await conn.fetchval("SELECT patient_id FROM prescription_files WHERE filename='legado.pdf'"),"paciente")
            self.assertIsNone(await conn.fetchval("SELECT patient_id FROM prescription_files WHERE filename='ambiguo.pdf'"))

    async def test_firebase_reconciliation_persists_missing_then_purges_without_touching_other_account(self):
        from services.firebase_account_reconciliation import reconcile_accounts
        exists = AsyncMock(return_value=True)
        await reconcile_accounts(self.pool, exists=exists)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_auth_observations WHERE observed_active_at IS NOT NULL"), 2)
        exists.side_effect = lambda uid: uid != "paciente"
        await reconcile_accounts(self.pool, exists=exists)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles"), 2)
            await conn.execute("UPDATE account_auth_observations SET missing_since=NOW()-interval '6 minutes' WHERE user_id='paciente'")
            await conn.execute("INSERT INTO feedbacks(user_id,category,message) VALUES ('paciente','bug','Sintético')")
        with tempfile.TemporaryDirectory() as root, patch.object(account_deletion, "STORAGE_DIR", root), patch(
            "services.firebase_account_reconciliation.cleanup_legacy_firebase_storage", AsyncMock()
        ), patch("services.firebase_account_reconciliation.account_exists", AsyncMock(return_value=False)
        ):
            result = await reconcile_accounts(self.pool, exists=exists)
        self.assertEqual(result["purged"], 1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles WHERE id='paciente'"), 0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles WHERE id='cuidador'"), 1)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM feedbacks WHERE user_id='paciente'"), 0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_auth_observations WHERE user_id='paciente'"), 0)

    async def test_firebase_cleanup_failure_is_retained_and_retry_recovers(self):
        from services.firebase_account_reconciliation import cleanup_legacy_firebase_storage
        with tempfile.TemporaryDirectory() as root, patch.object(account_deletion, "STORAGE_DIR", root), patch(
            "services.firebase_account_reconciliation.cleanup_legacy_firebase_storage",
            AsyncMock(side_effect=RuntimeError("synthetic 503"))
        ), patch("services.firebase_account_reconciliation.account_exists", AsyncMock(return_value=False)
        ):
            result = await account_deletion.delete_account(self.pool, "paciente", actor="synthetic", firebase_cleanup=True)
            self.assertFalse(result["success"])
            async with self.pool.acquire() as conn:
                self.assertTrue(await conn.fetchval("SELECT firebase_cleanup_required FROM account_storage_cleanup WHERE user_id='paciente'"))
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM profiles WHERE id='paciente'"), 0)
            with patch("services.firebase_account_reconciliation.cleanup_legacy_firebase_storage", AsyncMock(spec=cleanup_legacy_firebase_storage)):
                self.assertTrue(await account_deletion.cleanup_account_files(self.pool, "paciente"))
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM account_storage_cleanup WHERE user_id='paciente'"), 0)
