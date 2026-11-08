"""Reações e caixa na mesma transação do PostgreSQL efêmero do CI."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
from fastapi import HTTPException
from routers import sync
import test_privacy_postgres as privacy


@unittest.skipUnless(privacy.TEST_DSN, "PostgreSQL efêmero disponível somente no CI")
class NotificationReactionsPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await privacy.PrivacyPostgres.asyncSetUp(self)
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO profiles(id,full_name) VALUES ('outro','Outro')")
            self.post = await conn.fetchval("""INSERT INTO social_posts(user_id,post_type,title)
                VALUES ('paciente','milestone','Título que não pertence ao payload') RETURNING id""")
        self.state_patch = patch.object(sync, "_state", SimpleNamespace(
            db_pool=self.pool, rate_limiter=MagicMock(check=AsyncMock())))
        self.state_patch.start()

    async def asyncTearDown(self):
        self.state_patch.stop()
        await privacy.PrivacyPostgres.asyncTearDown(self)

    async def react(self, uid="cuidador", kind="apoio"):
        return await sync.sync_post_reaction(sync.PostReactionSyncDto(
            postId=self.post, userId=uid, reactionType=kind,
            createdAt=datetime.now(timezone.utc).isoformat()), uid=uid)

    async def item(self):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT id,revision,snapshot FROM notification_items
                WHERE recipient_id='paciente' AND group_key=$1""", f"post:{self.post}")
        return row["id"], row["revision"], json.loads(row["snapshot"])

    async def test_retry_change_and_multiple_actors_keep_one_group_and_real_read_state(self):
        await asyncio.gather(self.react(), self.react())
        item_id, revision, data = await self.item()
        self.assertEqual((1, 1, False), (data["count"], data["unreadCount"], data["isRead"]))
        self.assertEqual({"reaction": "apoio"}, data["data"])
        await self.react()
        self.assertEqual(revision, (await self.item())[1])
        async with self.pool.acquire() as conn:
            self.assertEqual(1, await conn.fetchval("SELECT COUNT(*) FROM notification_outbox"))
            await conn.execute("SELECT notification_mark($1,$2,$3)", "paciente", item_id, revision)
        await self.react(kind="palmas")
        changed = (await self.item())[2]
        self.assertTrue(changed["isRead"])
        self.assertEqual(0, changed["unreadCount"])
        self.assertEqual({"reaction": "palmas"}, changed["data"])
        async with self.pool.acquire() as conn:
            self.assertEqual(1, await conn.fetchval("SELECT COUNT(*) FROM notification_outbox"))
        await self.react(uid="outro", kind="forca")
        new_id, _, grouped = await self.item()
        self.assertEqual(item_id, new_id)
        self.assertEqual((2, 1, False), (grouped["count"], grouped["unreadCount"], grouped["isRead"]))
        async with self.pool.acquire() as conn:
            self.assertEqual(["paciente"], [r["recipient_id"] for r in await conn.fetch("SELECT recipient_id FROM notification_items")])
            self.assertEqual(2, await conn.fetchval("SELECT COUNT(*) FROM notification_outbox"))

    async def test_removal_updates_group_and_recreation_reuses_item_with_fresh_event(self):
        await self.react()
        original_id, _, original = await self.item()
        await self.react(uid="outro")
        await sync.delete_post_reaction(self.post, uid="cuidador")
        item_id, revision, data = await self.item()
        self.assertEqual(original_id, item_id)
        self.assertEqual((1, "outro"), (data["count"], data["actorId"]))
        await sync.delete_post_reaction(self.post, uid="cuidador")
        self.assertEqual(revision, (await self.item())[1])
        await sync.delete_post_reaction(self.post, uid="outro")
        self.assertTrue((await self.item())[2]["deleted"])
        await self.react()
        item_id, _, recreated = await self.item()
        self.assertEqual(original_id, item_id)
        self.assertFalse(recreated["deleted"])
        self.assertFalse(recreated["isRead"])
        self.assertEqual(1, recreated["count"])
        self.assertGreater(recreated["occurredAt"], original["occurredAt"])
        async with self.pool.acquire() as conn:
            self.assertEqual(3, await conn.fetchval("SELECT COUNT(*) FROM notification_outbox"))

    async def test_self_reaction_never_notifies_and_family_audience_is_revalidated(self):
        await self.react(uid="paciente")
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval("SELECT COUNT(*) FROM notification_sources"))
            await conn.execute("UPDATE social_posts SET visibility='FAMILY' WHERE id=$1", self.post)
        with self.assertRaises(HTTPException) as inaccessible:
            await self.react()
        self.assertEqual(404, inaccessible.exception.status_code)
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO family_bindings(patient_id,caregiver_id,caregiver_name,
                caregiver_relation,connection_code,status,expires_at)
                VALUES ('paciente','cuidador','Teste','Familiar','NOTIF123','ACTIVE',now()+interval '1 day')""")
        await self.react()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='REVOKED' WHERE caregiver_id='cuidador'")
            await conn.execute("SELECT notification_prune('paciente')")
        self.assertTrue((await self.item())[2]["deleted"])
        with self.assertRaises(HTTPException):
            await self.react(kind="palmas")

    async def test_block_is_transactional_and_unblocking_does_not_resurrect_old_event(self):
        await self.react()
        await sync.block_social_user("cuidador", uid="paciente")
        self.assertTrue((await self.item())[2]["deleted"])
        with self.assertRaises(HTTPException) as blocked:
            await self.react(kind="palmas")
        self.assertEqual(404, blocked.exception.status_code)
        await sync.unblock_social_user("cuidador", uid="paciente")
        await self.react()  # Retry idêntico do cache não cria outro aviso.
        self.assertTrue((await self.item())[2]["deleted"])
        async with self.pool.acquire() as conn:
            self.assertEqual(1, await conn.fetchval("SELECT COUNT(*) FROM notification_outbox"))

    async def test_concurrent_block_and_reaction_end_without_visible_source(self):
        reaction, block = await asyncio.gather(self.react(),
            sync.block_social_user("cuidador", uid="paciente"), return_exceptions=True)
        self.assertEqual({"status": "blocked"}, block)
        if isinstance(reaction, HTTPException):
            self.assertEqual(404, reaction.status_code)
        else:
            self.assertEqual({"status": "success"}, reaction)
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval("SELECT COUNT(*) FROM notification_sources WHERE active"))

    async def test_soft_delete_invalidates_group_and_external_moderation_is_pruned(self):
        await self.react()
        await sync.delete_social_post(self.post, uid="paciente")
        self.assertTrue((await self.item())[2]["deleted"])
        revision = (await self.item())[1]
        await sync.delete_social_post(self.post, uid="paciente")
        self.assertEqual(revision, (await self.item())[1])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE social_posts SET is_visible=TRUE WHERE id=$1", self.post)
        await self.react(kind="palmas")
        self.assertFalse((await self.item())[2]["deleted"])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE social_posts SET is_visible=FALSE WHERE id=$1", self.post)
            await conn.execute("SELECT notification_prune('paciente')")
        self.assertTrue((await self.item())[2]["deleted"])

    async def test_outbox_failure_rolls_back_reaction_and_entire_notification(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE FUNCTION reject_reaction_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'falha sintética de outbox'; END $$""")
            await conn.execute("CREATE TRIGGER reject_reaction_outbox BEFORE INSERT ON notification_outbox FOR EACH ROW EXECUTE FUNCTION reject_reaction_outbox()")
        with self.assertRaises(asyncpg.PostgresError):
            await self.react()
        async with self.pool.acquire() as conn:
            for table in ("post_reactions", "notification_items", "notification_sources", "notification_outbox"):
                self.assertEqual(0, await conn.fetchval(f"SELECT COUNT(*) FROM {table}"))

    async def test_invalidation_failure_rolls_back_post_delete_and_user_block(self):
        await self.react()
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE FUNCTION reject_reaction_removal() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NOT NEW.active THEN RAISE EXCEPTION 'falha sintética de remoção'; END IF; RETURN NEW; END $$""")
            await conn.execute("CREATE TRIGGER reject_reaction_removal BEFORE UPDATE ON notification_sources FOR EACH ROW EXECUTE FUNCTION reject_reaction_removal()")
        with self.assertRaises(asyncpg.PostgresError):
            await sync.delete_social_post(self.post, uid="paciente")
        with self.assertRaises(asyncpg.PostgresError):
            await sync.block_social_user("cuidador", uid="paciente")
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval("SELECT is_visible FROM social_posts WHERE id=$1", self.post))
            self.assertEqual(0, await conn.fetchval("SELECT COUNT(*) FROM social_user_blocks"))
            self.assertEqual(1, await conn.fetchval("SELECT COUNT(*) FROM notification_sources WHERE active"))
