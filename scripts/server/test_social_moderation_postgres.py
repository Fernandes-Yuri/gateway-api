"""SPEC-09: denúncia, bloqueio, revisão concorrente e rollback no PostgreSQL do CI."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from routers import admin, sync
import test_privacy_postgres as privacy
from test_moderation_postgres import ModerationPostgres


@unittest.skipUnless(privacy.TEST_DSN, 'PostgreSQL efêmero disponível somente no CI')
class SocialModerationPostgres(unittest.IsolatedAsyncioTestCase):
    request = ModerationPostgres.request

    async def asyncSetUp(self):
        await ModerationPostgres.asyncSetUp(self)
        async with self.pool.acquire() as conn:
            for number in (23, 27, 47, 77):
                migration = next((privacy.ROOT / 'migrations').glob(f'{number:03d}_*.sql'))
                await conn.execute(migration.read_text(encoding='utf-8'))
            await conn.execute("INSERT INTO profiles(id,full_name) VALUES ('terceiro','Terceiro')")
            self.post = await conn.fetchval("INSERT INTO social_posts(user_id,post_type,title) VALUES ('paciente','milestone','Conquista') RETURNING id")
        self.sync_state = patch.object(sync, '_state', SimpleNamespace(db_pool=self.pool, rate_limiter=MagicMock(check=AsyncMock())))
        self.sync_state.start()

    async def asyncTearDown(self):
        self.sync_state.stop()
        await ModerationPostgres.asyncTearDown(self)

    async def report(self, uid='cuidador'):
        return await sync.report_social_post(self.post, sync.SocialReportDto(reason='spam'), uid=uid)

    async def action(self, report_id, action):
        return await admin.act_on_social_report(report_id, admin.SocialReportAction(action=action),
            self.request('POST', f'/api/admin/social-reports/{report_id}/action'))

    async def test_reports_are_unique_even_concurrently_and_private_posts_are_protected(self):
        await asyncio.gather(self.report(), self.report())
        async with self.pool.acquire() as conn:
            self.assertEqual(1, await conn.fetchval('SELECT COUNT(*) FROM social_reports'))
            self.assertEqual('pending', await conn.fetchval('SELECT status FROM social_reports'))
            await conn.execute("UPDATE social_posts SET visibility='FAMILY' WHERE id=$1", self.post)
        for uid, expected in [('paciente', 403), ('terceiro', 404)]:
            with self.assertRaises(HTTPException) as caught:
                await self.report(uid)
            self.assertEqual(expected, caught.exception.status_code)

    async def test_block_hides_both_directions_and_unblock_does_not_remove_reverse_block(self):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO social_posts(user_id,post_type,title) VALUES ('cuidador','milestone','Outra')")
        for _ in range(2):
            await sync.block_social_user('paciente', uid='cuidador')
        for viewer, hidden in [('cuidador', 'paciente'), ('paciente', 'cuidador')]:
            feed = await sync.get_social_feed(limit=30, offset=0, uid=viewer)
            self.assertNotIn(hidden, [r['user_id'] for r in feed])
        with self.assertRaises(HTTPException):
            await sync.sync_post_reaction(sync.PostReactionSyncDto(postId=self.post, userId='cuidador', createdAt='2026-10-30T12:00:00Z'), uid='cuidador')
        await sync.block_social_user('cuidador', uid='paciente')
        await sync.unblock_social_user('paciente', uid='cuidador')
        self.assertNotIn('paciente', [r['user_id'] for r in await sync.get_social_feed(limit=30, offset=0, uid='cuidador')])
        await sync.unblock_social_user('cuidador', uid='paciente')
        self.assertIn('paciente', [r['user_id'] for r in await sync.get_social_feed(limit=30, offset=0, uid='cuidador')])

    async def test_remove_resolves_all_pending_reports_and_retries_do_not_duplicate_audit(self):
        await self.report()
        await self.report('terceiro')
        listing = await admin.list_social_reports(self.request('GET', '/api/admin/social-reports'), limit=1, offset=0)
        self.assertTrue(listing['has_more'])
        self.assertEqual('Conquista', listing['data'][0]['title'])
        report_id = listing['data'][0]['id']
        await asyncio.gather(self.action(report_id, 'remove_post'), self.action(report_id, 'remove_post'))
        async with self.pool.acquire() as conn:
            self.assertFalse(await conn.fetchval('SELECT is_visible FROM social_posts WHERE id=$1', self.post))
            self.assertEqual(2, await conn.fetchval("SELECT COUNT(*) FROM social_reports WHERE status='removed' AND reviewed_by='admin@example.test' AND reviewed_at IS NOT NULL"))
            self.assertEqual(1, await conn.fetchval('SELECT COUNT(*) FROM moderation_audit_log'))
        self.assertEqual([], await sync.get_social_feed(limit=30, offset=0, uid='terceiro'))

    async def test_dismiss_keeps_post_and_failed_audit_rolls_back_removal(self):
        await self.report()
        await self.report('terceiro')
        async with self.pool.acquire() as conn:
            reports = await conn.fetch('SELECT id FROM social_reports ORDER BY id')
        await self.action(reports[0]['id'], 'dismiss')
        with self.assertRaises(HTTPException) as conflict:
            await self.action(reports[0]['id'], 'remove_post')
        self.assertEqual(409, conflict.exception.status_code)
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'teste'; END $$")
            await conn.execute('CREATE TRIGGER reject_audit BEFORE INSERT ON moderation_audit_log FOR EACH ROW EXECUTE FUNCTION reject_audit()')
        with self.assertRaises(Exception):
            await self.action(reports[1]['id'], 'remove_post')
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT is_visible FROM social_posts WHERE id=$1', self.post))
            self.assertEqual('pending', await conn.fetchval('SELECT status FROM social_reports WHERE id=$1', reports[1]['id']))

    async def test_account_deletion_cascades_reports_and_blocks(self):
        await self.report()
        await sync.block_social_user('paciente', uid='cuidador')
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM profiles WHERE id='cuidador'")
            self.assertEqual(0, await conn.fetchval('SELECT COUNT(*) FROM social_reports'))
            self.assertEqual(0, await conn.fetchval('SELECT COUNT(*) FROM social_user_blocks'))
            self.assertTrue(await conn.fetchval('SELECT is_visible FROM social_posts WHERE id=$1', self.post))
