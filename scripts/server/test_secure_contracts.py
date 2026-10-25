"""Regressões de autorização, persistência e exclusão da auditoria."""
import tempfile
import json
from io import BytesIO
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from routers import admin, care_os, sync
from security_defense import StrictRateLimiter
from services import account_deletion
from services import prescription_analyzer
from services.prescription_analyzer import ReconciliationResult


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class SecureContracts(unittest.IsolatedAsyncioTestCase):
    async def test_failed_prescription_persistence_removes_file(self):
        conn = MagicMock(execute=AsyncMock(side_effect=RuntimeError("falha de persistência")))
        with tempfile.TemporaryDirectory() as root, patch.object(care_os._state,"db_pool",Pool(conn)), patch.object(
            care_os,"PRESCRIPTION_STORAGE_DIR",root
        ), patch.object(care_os,"extract_raw_ocr_text",AsyncMock(return_value="texto")), patch.object(
            care_os,"extract_structured_prescription",AsyncMock(return_value=[])
        ), patch.object(care_os,"reconcile_and_verify",AsyncMock(return_value=ReconciliationResult(medications=[]))):
            file = UploadFile(filename="foto.png",file=BytesIO(b"imagem"),headers=Headers({"content-type":"image/png"}))
            with self.assertRaises(RuntimeError):
                await care_os.analyze_prescription("paciente",file=file,uid="paciente")
            self.assertEqual(list(Path(root).iterdir()),[])

    async def test_pdf_missing_posology_requires_manual_fill_without_times(self):
        with patch.object(prescription_analyzer,"GEMINI_API_KEY","chave de teste"), patch.object(
            prescription_analyzer,"_call_gemini_vision",AsyncMock(return_value=json.dumps([{"name":"Atenolol","dosage":"25mg"}]))
        ):
            result = await prescription_analyzer.analyze_pdf_prescription(b"%PDF falso",MagicMock(fetch=AsyncMock(return_value=[])),raw_ocr_text="Atenolol 25mg")
        self.assertTrue(result.medications[0].requires_human_fill)
        self.assertTrue(result.medications[0].frequency_divergent)
        self.assertEqual(result.medications[0].suggested_times,[])
        self.assertEqual(result.medications[0].confidence_score,0)

    async def test_different_posology_is_not_merged_as_duplicate_copy(self):
        result = await prescription_analyzer.reconcile_and_verify(
            [{"name":"Atenolol","dosage":"25mg","frequency":frequency} for frequency in ("1x ao dia","12/12h")],
            "Atenolol 25mg 1x ao dia; Atenolol 25mg 12/12h",MagicMock(fetch=AsyncMock(return_value=[])))
        self.assertEqual(len(result.medications),2)
        self.assertEqual(result.duplicates_merged_count,0)

    async def test_block_duration_is_shared_with_live_cache(self):
        conn = MagicMock(execute=AsyncMock())
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        limiter = MagicMock(set_manual_block=AsyncMock())
        with patch.object(admin._state,"db_pool",Pool(conn)), patch.object(admin._state,"rate_limiter",limiter), patch.object(admin, '_moderation_operator', AsyncMock(return_value='admin@example.test')):
            from starlette.requests import Request
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/admin/blocked-ips', 'headers': []})
            await admin.create_blocked_ip(admin.BlockedIpCreate(ip="203.0.113.5",reason="teste",minutes=60), request)
        self.assertEqual(conn.execute.call_args.args[-1],60)
        limiter.set_manual_block.assert_awaited_once_with("203.0.113.5",True,seconds=3600)

    async def test_active_block_check_queries_authoritative_table(self):
        conn = MagicMock(fetchval=AsyncMock(return_value=True))
        with patch.object(admin._state,"db_pool",Pool(conn)), patch.object(admin, '_moderation_operator', AsyncMock(return_value='admin@example.test')):
            self.assertEqual(await admin.check_blocked_ip(MagicMock(), "203.0.113.5"),{"blocked":True})
        self.assertIn("blocked_until > NOW()",conn.fetchval.call_args.args[0])

    async def test_patient_owner_and_linked_caregiver_can_read_but_intruder_cannot(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root,"receita.png").write_bytes(b"imagem de teste")
            for uid, binding, allowed in (
                ("paciente",None,True),
                ("cuidador",{"caregiver_role":"CAREGIVER_VIEWER","permissions":[]},True),
                ("intruso",None,False),
                ("revogado",None,False),
            ):
                conn = MagicMock(fetchval=AsyncMock(return_value="paciente"),fetchrow=AsyncMock(return_value=binding))
                with patch.object(care_os._state,"db_pool",Pool(conn)), patch.object(care_os,"PRESCRIPTION_STORAGE_DIR",root):
                    if allowed:
                        response = await care_os.prescription_photo("receita.png",uid=uid)
                        self.assertEqual(response.path,str(Path(root,"receita.png")))
                    else:
                        with self.assertRaises(HTTPException) as error:
                            await care_os.prescription_photo("receita.png",uid=uid)
                        self.assertEqual(error.exception.status_code,403)

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
        self.assertIn(("DELETE FROM device_push_tokens WHERE user_id=$1","firebase-uid"),sql_calls)
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
        with tempfile.TemporaryDirectory() as root, patch.object(account_deletion,"STORAGE_DIR",root), patch.object(account_deletion.os.path,"isdir",return_value=True), patch.object(
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
