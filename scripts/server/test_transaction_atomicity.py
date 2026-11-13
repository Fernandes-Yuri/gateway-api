"""
Teste de Atomicidade Transacional: Força erro no meio da operação e comprova rollback.
Arquivo: scripts/server/test_transaction_atomicity.py
"""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

server_dir = Path(__file__).resolve().parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import care_os


class _MockPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _create_test_client(conn, uid: str | None = None):
    app = FastAPI()
    care_os.configure_state(db_pool=_MockPool(conn), public_base_url="https://api.bragasaude.online")
    app.include_router(care_os.router)
    if uid:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app, raise_server_exceptions=False)


class TestTransactionAtomicity(unittest.TestCase):
    def setUp(self):
        self.patient_id = "patient-atomicity-001"
        self.caregiver_id = "caregiver-atomicity-002"
        self.med_id = "550e8400-e29b-41d4-a716-446655440000"

    def _setup_base_conn(self, med_row=None):
        conn = MagicMock()
        tx_mock = AsyncMock()
        conn.transaction.return_value.__aenter__ = AsyncMock(return_value=tx_mock)
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

        async def mock_fetchrow(query, *args):
            q = query.lower()
            if "from family_bindings" in q:
                return {"caregiver_role": "ADMIN_CHILD", "permissions": []}
            if "from medications" in q:
                return med_row
            return None

        conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
        conn.fetchval = AsyncMock(return_value="Dra. Maria Cuidadora")
        conn.execute = AsyncMock(return_value="OK")
        return conn

    def test_delete_medication_success_runs_inside_transaction(self):
        """Verifica que na exclusão bem-sucedida a transação é aberta e confirmada."""
        med_row = {
            "id": self.med_id,
            "name": "Losartana 50mg",
            "photo_reference_url": None,
        }
        conn = self._setup_base_conn(med_row=med_row)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["deleted"])

        # Prova que o bloco conn.transaction() foi iniciado e fechado com sucesso
        conn.transaction.assert_called_once()
        conn.transaction.return_value.__aenter__.assert_called_once()
        conn.transaction.return_value.__aexit__.assert_called_once()

        exit_args = conn.transaction.return_value.__aexit__.call_args.args
        self.assertIsNone(exit_args[0], "Transação com sucesso não pode conter erro no __aexit__")

    @patch("routers.care_os._try_remove_stored_file")
    def test_delete_medication_midway_failure_rolls_back_and_skips_external_effects(self, mock_remove_file):
        """
        Força um erro catastrófico durante a execução da trilha LGPD (passo 3 dentro da transação).
        Comprova que:
        1. A requisição é interrompida com 500.
        2. A transação captura a exceção em __aexit__, realizando o ROLLBACK.
        3. Efeitos externos fora do banco (_try_remove_stored_file) NUNCA são acionados.
        """
        med_row = {
            "id": self.med_id,
            "name": "Losartana 50mg",
            "photo_reference_url": "https://api.bragasaude.online/api/files/prescriptions/receita_123.pdf",
        }
        conn = self._setup_base_conn(med_row=med_row)

        executed_queries = []

        async def mock_execute_with_midway_failure(query, *args):
            q = query.lower()
            executed_queries.append(q)
            if "delete from medication_logs" in q:
                return "DELETE 5"
            if "delete from medications" in q:
                return "DELETE 1"
            if "insert into care_audit_trail" in q:
                # Simula falha crítica no meio da operação (ex: erro de constraint, disco cheio, falha no RDS)
                raise RuntimeError("FALHA FORÇADA: Simulação de queda/falha no meio da transação de exclusão")
            return "OK"

        conn.execute = AsyncMock(side_effect=mock_execute_with_midway_failure)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")

        # 1. Prova que a requisição abortou com status 500
        self.assertEqual(response.status_code, 500)

        # 2. Prova que as queries anteriores foram disparadas na transação antes de quebrar
        self.assertTrue(any("delete from medication_logs" in q for q in executed_queries))
        self.assertTrue(any("delete from medications" in q for q in executed_queries))
        self.assertTrue(any("insert into care_audit_trail" in q for q in executed_queries))

        # 3. Prova que o context manager da transação interceptou a falha forçada, disparando o ROLLBACK
        conn.transaction.return_value.__aexit__.assert_called_once()
        exit_args = conn.transaction.return_value.__aexit__.call_args.args
        self.assertIsNotNone(exit_args[0], "O __aexit__ deve receber a exceção para executar o ROLLBACK no banco")
        self.assertEqual(exit_args[0], RuntimeError)
        self.assertIn("FALHA FORÇADA", str(exit_args[1]))

        # 4. Prova que a limpeza de arquivos externos em disco NUNCA foi chamada após a falha
        mock_remove_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
