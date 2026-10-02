"""Regressões de autorização, persistência e exclusão da auditoria."""
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from routers import care_os, sync
from security_defense import StrictRateLimiter
from services import account_deletion


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class SecureContracts(unittest.IsolatedAsyncioTestCase):
    async def test_account_deletion_uses_firebase_string_and_transaction(self):
        conn = MagicMock(fetchrow=AsyncMock(return_value={"id":"firebase-uid"}),
                         fetch=AsyncMock(return_value=[]), execute=AsyncMock())
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(account_deletion,"cleanup_account_files",AsyncMock(return_value=True)):
            result = await account_deletion.delete_account(Pool(conn),"firebase-uid",actor="titular")
        self.assertTrue(result["deleted"])
        conn.transaction.assert_called_once()
        sql_calls = [call.args for call in conn.execute.await_args_list]
        self.assertIn(("DELETE FROM profiles WHERE id=$1","firebase-uid"),sql_calls)
        self.assertTrue(any("user_deletion_logs" in call[0] for call in sql_calls))

    async def test_database_failure_prevents_storage_cleanup(self):
        conn = MagicMock(fetchrow=AsyncMock(return_value={"id":"uid"}),
                         fetch=AsyncMock(return_value=[]),execute=AsyncMock(side_effect=RuntimeError("falha de escrita")))
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(account_deletion,"cleanup_account_files",AsyncMock()) as cleanup:
            with self.assertRaises(RuntimeError):
                await account_deletion.delete_account(Pool(conn),"uid",actor="titular")
        cleanup.assert_not_awaited()
        self.assertEqual(conn.transaction.return_value.__aexit__.call_args.args[0],RuntimeError)

    async def test_prescription_foreign_patient_denied(self):
        conn = MagicMock(fetchval=AsyncMock(return_value="paciente-a"))
        with patch.object(care_os._state, "db_pool", Pool(conn)), patch.object(
            care_os, "require_patient_permission", AsyncMock(side_effect=HTTPException(403, "Sem vínculo"))
        ) as permission:
            with self.assertRaises(HTTPException) as error:
                await care_os.prescription_photo("anexo.pdf", uid="usuario-b")
        self.assertEqual(error.exception.status_code, 403)
        permission.assert_awaited_once_with(conn, "usuario-b", "paciente-a", "care:read")

    async def test_unowned_legacy_prescription_is_not_public(self):
        conn = MagicMock(fetchval=AsyncMock(return_value=None))
        with patch.object(care_os._state, "db_pool", Pool(conn)):
            with self.assertRaises(HTTPException) as error:
                await care_os.prescription_photo("legado.pdf", uid="usuario")
        self.assertEqual(error.exception.status_code, 404)

    async def test_feedback_offline_does_not_acknowledge(self):
        with patch.object(sync._state, "db_pool", None):
            with self.assertRaises(HTTPException) as error:
                await sync.sync_feedback(sync.FeedbackSyncDto(userId="u",category="bug",message="mensagem"), MagicMock(), uid="u")
        self.assertEqual(error.exception.status_code, 503)

    async def test_feedback_retry_keeps_id_and_checks_owner(self):
        fid = uuid.uuid4()
        conn = MagicMock(fetchrow=AsyncMock(side_effect=[None, {"id": fid}]))
        with patch.object(sync._state, "db_pool", Pool(conn)), patch.object(
            sync._state, "rate_limiter", MagicMock(check=AsyncMock())
        ), patch.object(sync, "get_client_ip", return_value="unknown"):
            response = await sync.sync_feedback(sync.FeedbackSyncDto(id=str(fid),userId="u",category="bug",message="mensagem"), MagicMock(), uid="u")
        self.assertTrue(response["persisted"])
        self.assertEqual(response["id"], str(fid))
        self.assertEqual(conn.fetchrow.call_args.args[1:], (fid,"u"))

    async def test_failed_storage_cleanup_keeps_durable_job(self):
        conn = MagicMock(fetchrow=AsyncMock(return_value={"prescription_filenames": []}),execute=AsyncMock())
        with tempfile.TemporaryDirectory() as root, patch.object(account_deletion,"STORAGE_DIR",root), patch.object(
            account_deletion,"STORAGE_EXAMS_DIR",str(Path(root)/"exams")
        ), patch.object(account_deletion.os.path,"isdir",return_value=True), patch.object(
            account_deletion.shutil,"rmtree",side_effect=OSError("arquivo ocupado")
        ):
            self.assertFalse(await account_deletion.cleanup_account_files(Pool(conn),"firebase-uid"))
        conn.execute.assert_not_called()

    async def test_manual_block_updates_and_clears_live_limiter(self):
        limiter = StrictRateLimiter()
        await limiter.set_manual_block("203.0.113.5",True)
        self.assertTrue(limiter.is_ip_blocked("203.0.113.5")[0])
        await limiter.set_manual_block("203.0.113.5",False)
        self.assertFalse(limiter.is_ip_blocked("203.0.113.5")[0])
