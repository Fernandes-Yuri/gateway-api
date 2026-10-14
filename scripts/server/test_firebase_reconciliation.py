"""DT-001: falhas nunca viram prova de ausência e perfis históricos ficam intactos."""
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from firebase_admin import auth
from services import firebase_account_reconciliation as worker


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def row(self, **overrides):
        return {"user_id": "synthetic-uid", "observed_active_at": datetime.now(timezone.utc),
                "missing_since": None, **overrides}

    async def run_case(self, row, exists):
        conn = MagicMock(execute=AsyncMock(), fetch=AsyncMock(return_value=[row]),
                         fetchval=AsyncMock(return_value=True))
        delete = AsyncMock(return_value={"success": True})
        stats = await worker.reconcile_accounts(Pool(conn), exists=exists, delete=delete)
        return stats, conn, delete

    async def test_active_account_is_observed_not_deleted(self):
        stats, conn, delete = await self.run_case(self.row(), AsyncMock(return_value=True))
        self.assertEqual(stats["active"], 1)
        delete.assert_not_awaited()
        self.assertTrue(any("observed_active_at=NOW()" in c.args[0] for c in conn.execute.await_args_list))

    async def test_unknown_historical_orphan_is_never_deleted(self):
        stats, _, delete = await self.run_case(self.row(observed_active_at=None), AsyncMock(return_value=False))
        self.assertEqual(stats["historical_unconfirmed"], 1)
        delete.assert_not_awaited()

    async def test_first_missing_observation_does_not_delete(self):
        stats, _, delete = await self.run_case(self.row(), AsyncMock(return_value=False))
        self.assertEqual(stats["missing"], 1)
        delete.assert_not_awaited()

    async def test_two_missing_observations_with_grace_and_final_check_delete_once(self):
        stats, _, delete = await self.run_case(
            self.row(missing_since=datetime.now(timezone.utc) - timedelta(minutes=6)),
            AsyncMock(return_value=False))
        self.assertEqual(stats["purged"], 1)
        delete.assert_awaited_once()
        self.assertEqual(delete.await_args.kwargs["firebase_cleanup"], True)

    async def test_recreated_account_before_final_check_is_preserved(self):
        stats, _, delete = await self.run_case(
            self.row(missing_since=datetime.now(timezone.utc) - timedelta(minutes=6)),
            AsyncMock(side_effect=[False, True]))
        self.assertEqual(stats["active"], 1)
        delete.assert_not_awaited()

    async def test_timeout_503_permissions_never_count_as_missing(self):
        for failure in (TimeoutError(), RuntimeError("503"), PermissionError()):
            stats, conn, delete = await self.run_case(
                self.row(missing_since=datetime.now(timezone.utc) - timedelta(minutes=6)),
                AsyncMock(side_effect=failure))
            self.assertEqual(stats["unavailable"], 1)
            self.assertEqual(stats["missing"], 0)
            delete.assert_not_awaited()
            self.assertTrue(any("missing_since=NULL" in c.args[0] for c in conn.execute.await_args_list))

    async def test_another_worker_holding_lock_prevents_duplicate_work(self):
        conn = MagicMock(fetchval=AsyncMock(return_value=False), execute=AsyncMock())
        result = await worker.reconcile_accounts(Pool(conn))
        self.assertTrue(result["busy"])
        conn.execute.assert_not_awaited()

    async def test_only_user_not_found_is_absence_and_project_is_checked(self):
        with patch.object(worker, "require_project") as project, patch.object(
            auth, "get_user", side_effect=auth.UserNotFoundError("synthetic")):
            self.assertFalse(await worker.account_exists("synthetic-uid"))
            project.assert_called_once()
        with patch.object(worker, "require_project"), patch.object(auth, "get_user", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                await worker.account_exists("synthetic-uid")

    async def test_wrong_project_disables_reconciliation(self):
        with patch.object(worker.firebase_admin, "get_app", return_value=MagicMock(project_id="wrong-project")):
            with self.assertRaises(RuntimeError):
                await worker.account_exists("synthetic-uid")

    async def test_missing_bucket_is_clean_but_permission_failure_is_not(self):
        bucket = MagicMock(exists=MagicMock(return_value=False), list_blobs=MagicMock())
        with patch.object(worker, "require_project"), patch.object(worker.storage, "bucket", return_value=bucket):
            await worker.cleanup_legacy_firebase_storage("synthetic-uid")
        bucket.list_blobs.assert_not_called()
        bucket.exists.side_effect = PermissionError()
        with patch.object(worker, "require_project"), patch.object(worker.storage, "bucket", return_value=bucket):
            with self.assertRaises(PermissionError):
                await worker.cleanup_legacy_firebase_storage("synthetic-uid")

    async def test_storage_prefixes_do_not_match_other_uid(self):
        bucket = MagicMock(exists=MagicMock(return_value=True), list_blobs=MagicMock(return_value=[]))
        with patch.object(worker, "require_project"), patch.object(worker.storage, "bucket", return_value=bucket):
            await worker.cleanup_legacy_firebase_storage("synthetic-uid")
        self.assertEqual([c.kwargs["prefix"] for c in bucket.list_blobs.call_args_list],
                         ["exams/synthetic-uid/", "uploads/synthetic-uid/"])
