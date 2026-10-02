"""Testes automatizados do Ciclo de Vida do Medicamento: Exclusão (LGPD Art. 18) e Edição (PATCH)."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# Garante que scripts/server esteja no sys.path
server_dir = Path(__file__).resolve().parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from config import STORAGE_DIR
from routers import care_os
from schemas.medications import MedicationUpdateRequest


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
    if uid is not None:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app)


class TestCareOsMedicationCrud(unittest.TestCase):
    def setUp(self):
        self.patient_id = "patient-001"
        self.caregiver_id = "caregiver-001"
        self.med_id = "00000000-0000-0000-0000-000000000001"

    def _setup_conn_mock(self, caregiver_role="ADMIN_CHILD", med_row=None):
        conn = MagicMock()
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

        # Simulação de consultas fetchrow
        async def mock_fetchrow(query, *args):
            q = query.lower()
            if "from family_bindings" in q:
                if caregiver_role is None:
                    return None
                return {"caregiver_role": caregiver_role, "permissions": []}
            if "from medications" in q:
                return med_row
            return None

        conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
        conn.fetchval = AsyncMock(return_value="Dra. Maria Cuidadora")
        conn.execute = AsyncMock(return_value="OK")
        return conn

    def test_delete_medication_success(self):
        """Exclusão bem-sucedida, cascata de logs e criação de trilha em care_audit_trail (LGPD Art. 18)."""
        med_row = {
            "id": self.med_id,
            "name": "Losartana 50mg",
            "photo_reference_url": "https://api.bragasaude.online/api/files/prescriptions/receita_123.pdf",
        }
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD", med_row=med_row)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["deleted"])
        self.assertEqual(data["medication_id"], self.med_id)
        self.assertEqual(data["patient_id"], self.patient_id)

        # Verifica execução de DELETE de logs, DELETE do medicamento e auditoria
        executed_queries = [call[0][0] for call in conn.execute.call_args_list]
        self.assertTrue(any("DELETE FROM medication_logs" in q for q in executed_queries))
        self.assertTrue(any("DELETE FROM medications" in q for q in executed_queries))
        self.assertTrue(any("INSERT INTO care_audit_trail" in q and "MEDICATION_DELETED" in q for q in executed_queries))

    def test_delete_medication_not_found(self):
        """Retorno 404 para ID inexistente ou sintaxe inválida de UUID."""
        # 1. ID inexistente no banco
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD", med_row=None)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Medicamento não encontrado", response.json()["detail"])

        # 2. Sintaxe não-UUID
        response_invalid = client.delete(f"/api/family/patients/{self.patient_id}/medications/nao-e-uuid")
        self.assertEqual(response_invalid.status_code, 404)

    def test_update_medication_schedule(self):
        """Atualização de schedule_times, meal_context e frequency_interval_hours via PATCH."""
        med_row = {
            "id": self.med_id,
            "name": "Losartana 50mg",
        }
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD", med_row=med_row)
        client = _create_test_client(conn, uid=self.caregiver_id)

        payload = {
            "schedule_times": ["08:00", "20:00"],
            "frequency_interval_hours": 12,
            "meal_context": "APOS_CAFE",
            "dosage_mg": 50.0,
            "treatment_duration_days": 30,
        }

        response = client.patch(
            f"/api/family/patients/{self.patient_id}/medications/{self.med_id}",
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["updated"])
        self.assertEqual(data["medication_id"], self.med_id)
        self.assertEqual(data["patient_id"], self.patient_id)

        # Verifica se o UPDATE no PostgreSQL contém as colunas atualizadas
        executed_queries = [call[0][0] for call in conn.execute.call_args_list]
        update_query = next((q for q in executed_queries if "UPDATE medications SET" in q), "")
        self.assertIn("schedule_times =", update_query)
        self.assertIn("schedule_time =", update_query)
        self.assertIn("frequency_interval_hours =", update_query)
        self.assertIn("meal_context =", update_query)
        self.assertIn("dosage_mg =", update_query)
        self.assertIn("treatment_duration_days =", update_query)
        self.assertIn("updated_at = now()", update_query)

        # Verifica inserção na trilha de auditoria
        audit_query = next((q for q in executed_queries if "INSERT INTO care_audit_trail" in q), "")
        self.assertIn("MEDICATION_UPDATED", audit_query)

    def test_update_medication_not_found(self):
        """Retorno 404 ao tentar atualizar medicamento inexistente."""
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD", med_row=None)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.patch(
            f"/api/family/patients/{self.patient_id}/medications/{self.med_id}",
            json={"name": "Novo Nome"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("Medicamento não encontrado", response.json()["detail"])

    def test_delete_and_update_unauthorized(self):
        """Retorno 403 se o usuário não possuir papel com permissão care:write."""
        # 1. Usuário sem vínculo (intruder)
        conn_intruder = self._setup_conn_mock(caregiver_role=None, med_row={"id": self.med_id, "name": "Remédio"})
        client_intruder = _create_test_client(conn_intruder, uid="intruder_user")

        del_resp = client_intruder.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(del_resp.status_code, 403)
        self.assertIn("não está autorizado", del_resp.json()["detail"])

        patch_resp = client_intruder.patch(
            f"/api/family/patients/{self.patient_id}/medications/{self.med_id}",
            json={"name": "Tentativa Invasão"},
        )
        self.assertEqual(patch_resp.status_code, 403)
        self.assertIn("não está autorizado", patch_resp.json()["detail"])

        # 2. Usuário com vínculo CAREGIVER_VIEWER (somente leitura care:read)
        conn_viewer = self._setup_conn_mock(caregiver_role="CAREGIVER_VIEWER", med_row={"id": self.med_id, "name": "Remédio"})
        client_viewer = _create_test_client(conn_viewer, uid="viewer_user")

        del_resp_viewer = client_viewer.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(del_resp_viewer.status_code, 403)
        self.assertIn("não permite esta operação", del_resp_viewer.json()["detail"])

        patch_resp_viewer = client_viewer.patch(
            f"/api/family/patients/{self.patient_id}/medications/{self.med_id}",
            json={"meal_context": "JEJUM"},
        )
        self.assertEqual(patch_resp_viewer.status_code, 403)
        self.assertIn("não permite esta operação", patch_resp_viewer.json()["detail"])

    def test_delete_medication_removes_physical_file(self):
        """Garante descarte físico do arquivo em disco (LGPD Art. 18)."""
        prescriptions_dir = os.path.join(STORAGE_DIR, "prescriptions")
        os.makedirs(prescriptions_dir, exist_ok=True)
        test_file_path = os.path.join(prescriptions_dir, "test_prescription_to_delete.pdf")
        with open(test_file_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")
        self.assertTrue(os.path.isfile(test_file_path))

        med_row = {
            "id": self.med_id,
            "name": "Clonazepam",
            "photo_reference_url": f"https://api.bragasaude.online/api/files/prescriptions/{os.path.basename(test_file_path)}",
        }
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD", med_row=med_row)
        conn.fetchval.side_effect = ["Cuidador", True]  # vínculo do anexo e ausência de outras referências
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.delete(f"/api/family/patients/{self.patient_id}/medications/{self.med_id}")
        self.assertEqual(response.status_code, 200)

        # Arquivo deve ter sido removido fisicamente
        self.assertFalse(os.path.isfile(test_file_path))

    def test_schema_sanitizes_null_strings(self):
        """Valida que MedicationUpdateRequest sanitiza strings 'null', 'undefined' e '' para None."""
        payload = MedicationUpdateRequest(
            name="null",
            meal_context="undefined",
            dosage_mg="none",
            frequency_interval_hours="",
        )
        self.assertIsNone(payload.name)
        self.assertIsNone(payload.meal_context)
        self.assertIsNone(payload.dosage_mg)
        self.assertIsNone(payload.frequency_interval_hours)


    def test_list_patient_medications_success(self):
        """Listagem completa dos medicamentos do paciente retorna 200 e dados estruturados."""
        meds = [
            {
                "id": self.med_id,
                "user_id": self.patient_id,
                "name": "Atenolol 25mg",
                "active_principle": "Atenolol",
                "manufacturer": "EMS",
                "dosage_mg": 25.0,
                "pharmaceutical_form": "Comprimido",
                "schedule_times": "08:00",
                "schedule_time": "08:00",
                "total_units": 30,
                "current_units": 20,
                "alert_threshold_days": 5,
                "photo_reference_url": None,
                "confirmed_with_prescription": True,
                "ean_barcode": "7896004715506",
                "anvisa_registration_number": "1023508490011",
                "meal_context": "APOS_CAFE",
                "frequency_interval_hours": 24,
                "treatment_duration_days": 60,
                "last_restock_date": None,
                "created_at": None,
                "updated_at": None,
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "user_id": self.patient_id,
                "name": "Losartana 50mg",
                "active_principle": "Losartana Potássica",
                "manufacturer": "Medley",
                "dosage_mg": 50.0,
                "pharmaceutical_form": "Comprimido",
                "schedule_times": "08:00,20:00",
                "schedule_time": "08:00",
                "total_units": 60,
                "current_units": 45,
                "alert_threshold_days": 7,
                "photo_reference_url": None,
                "confirmed_with_prescription": True,
                "ean_barcode": "7891234567890",
                "anvisa_registration_number": "1023508490022",
                "meal_context": None,
                "frequency_interval_hours": 12,
                "treatment_duration_days": 90,
                "last_restock_date": None,
                "created_at": None,
                "updated_at": None,
            },
        ]
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD")
        conn.fetch = AsyncMock(return_value=meds)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.get(f"/api/family/patients/{self.patient_id}/medications")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["name"], "Atenolol 25mg")
        self.assertEqual(data[0]["meal_context"], "APOS_CAFE")
        self.assertEqual(data[1]["name"], "Losartana 50mg")
        self.assertEqual(data[1]["frequency_interval_hours"], 12)

    def test_list_patient_medications_empty(self):
        """Listagem retorna lista vazia quando paciente não possui medicamentos cadastrados."""
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD")
        conn.fetch = AsyncMock(return_value=[])
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.get(f"/api/family/patients/{self.patient_id}/medications")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [])

    def test_list_patient_medications_unauthorized(self):
        """Retorno 403 se usuário sem vínculo tentar listar medicamentos."""
        conn = self._setup_conn_mock(caregiver_role=None)
        client = _create_test_client(conn, uid="intruder_user")

        response = client.get(f"/api/family/patients/{self.patient_id}/medications")
        self.assertEqual(response.status_code, 403)
        self.assertIn("não está autorizado", response.json()["detail"])

    def test_list_patient_medication_logs_success(self):
        """Logs de doses tomadas são retornados para sincronização com o banco local."""
        import datetime
        sample_logs = [
            {
                "id": "1973ac83-9c57-4f79-9b9e-6d70c8696659",
                "user_id": self.patient_id,
                "medication_id": self.med_id,
                "taken_at": datetime.datetime(2026, 9, 26, 14, 5, 8, tzinfo=datetime.timezone.utc),
                "scheduled_for": "2026-09-26T08:00:00-03:00",
                "actor_id": self.patient_id,
                "units_taken": 1,
                "idempotency_key": "idem-123",
            }
        ]
        conn = self._setup_conn_mock(caregiver_role="ADMIN_CHILD")
        conn.fetch = AsyncMock(return_value=sample_logs)
        client = _create_test_client(conn, uid=self.caregiver_id)

        response = client.get(f"/api/family/patients/{self.patient_id}/medications/logs?days=7")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "1973ac83-9c57-4f79-9b9e-6d70c8696659")
        self.assertEqual(data[0]["scheduled_for"], "2026-09-26T08:00:00-03:00")
        self.assertEqual(data[0]["units_taken"], 1)


if __name__ == "__main__":
    unittest.main()

