"""Testes de conformidade e integridade dos contratos de exames (Fase B/3).

Verifica as rotas /api/exams/upload, /api/exams/manual, /api/exams/{user_id} e /api/exams/{exam_id}
de acordo com o 08_CADERNO_DE_CONTRATOS_EXAMES_E_DOSSIE.md.
"""
import io
import os
import uuid
import unittest
from datetime import datetime, date, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from exams_api import install_exam_routes


class ExamsApiTests(unittest.TestCase):
    def setUp(self):
        self.user_id = "user_test_123"
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        self.app = FastAPI()
        install_exam_routes(self.app, lambda: self.pool)
        self.app.dependency_overrides[firebase_uid] = lambda: self.user_id
        self.client = TestClient(self.app)

    def test_legacy_sync_accepts_canonical_statuses(self):
        """Endpoints Android legados usam a mesma allowlist do módulo canônico."""
        for exam_status in ("uploaded", "analyzed", "confirmed", "failed"):
            exam_id = uuid.uuid4()
            self.conn.fetchrow.return_value = {"id": exam_id}
            response = self.client.post("/api/sync/exam", json={
                "id": str(exam_id),
                "userId": self.user_id,
                "title": "Hemograma",
                "category": "Laboratorial",
                "examDate": "2026-09-22",
                "status": exam_status,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "success")

    def test_legacy_sync_rejects_unknown_status(self):
        response = self.client.post("/api/sync/exam", json={
            "userId": self.user_id,
            "title": "Hemograma",
            "examDate": "2026-09-22",
            "status": "inventado",
        })
        self.assertEqual(response.status_code, 422)
        self.conn.fetchrow.assert_not_awaited()

    def test_legacy_sync_requires_owner(self):
        response = self.client.post("/api/sync/exam", json={
            "userId": "outra-conta",
            "title": "Hemograma",
            "examDate": "2026-09-22",
            "status": "uploaded",
        })
        self.assertEqual(response.status_code, 403)
        self.conn.fetchrow.assert_not_awaited()

    def test_post_manual_exam_success(self):
        """Valida inserção de exame digitado manualmente sem arquivo físico."""
        exam_id = str(uuid.uuid4())
        self.conn.fetchrow.return_value = {"id": uuid.UUID(exam_id)}

        payload = {
            "exam_id": exam_id,
            "title": "Hemograma e Glicemia",
            "category": "Laboratorial",
            "exam_date": "2026-09-17",
            "items": [
                {
                    "item_key": "glucose",
                    "item_name": "Glicose de Jejum",
                    "value_numeric": 95.0,
                    "value_text": "95",
                    "unit": "mg/dL"
                },
                {
                    "item_key": "creatinine",
                    "item_name": "Creatinina",
                    "value_numeric": 0.9,
                    "value_text": "0.9",
                    "unit": "mg/dL"
                }
            ]
        }

        response = self.client.post("/api/exams/manual", json=payload)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["exam_id"], exam_id)
        self.assertEqual(data["items_created_count"], 2)
        self.assertEqual(data["status"], "confirmed")

        # Verifica chamadas SQL
        self.conn.fetchrow.assert_called_once()
        self.assertEqual(self.conn.execute.call_count, 2)

    def test_upload_exam_with_cloud_consent_true(self):
        """Valida upload multipart com consentimento de nuvem ativo (CLOUD_STORED)."""
        exam_id = str(uuid.uuid4())
        self.conn.fetchrow.return_value = {"id": uuid.UUID(exam_id)}

        dummy_pdf = io.BytesIO(b"%PDF-1.4 Mock exam file content")

        data = {
            "title": "Ultrassonografia Abdominal",
            "category": "Imagem",
            "exam_date": "2026-09-17",
            "exam_type": "IMAGE_REPORT",
            "cloud_consent": "true",
            "terms_version": "v1.0_202609",
            "exam_id": exam_id
        }
        files = {
            "file": ("laudo.pdf", dummy_pdf, "application/pdf")
        }

        response = self.client.post("/api/exams/upload", data=data, files=files)
        self.assertEqual(response.status_code, 201)
        res = response.json()
        self.assertTrue(res["success"])
        self.assertEqual(res["storage_status"], "CLOUD_STORED")
        self.assertIn("/api/files/exams/", res["file_url"])

        # Deve registrar consentimento em exam_cloud_consents
        self.conn.execute.assert_called_once()
        self.conn.fetchrow.assert_called_once()

    def test_upload_exam_with_cloud_consent_false(self):
        """Se o consentimento for recusado, deve gravar LOCAL_ONLY e sem file_url no servidor."""
        exam_id = str(uuid.uuid4())
        self.conn.fetchrow.return_value = {"id": uuid.UUID(exam_id)}

        dummy_pdf = io.BytesIO(b"%PDF-1.4 Mock exam local only")

        data = {
            "title": "Exame Genético Confidencial",
            "category": "Genética",
            "exam_date": "2026-09-17",
            "exam_type": "IMAGE_REPORT",
            "cloud_consent": "false",
            "terms_version": "v1.0_202609",
            "exam_id": exam_id
        }
        files = {
            "file": ("genetica.pdf", dummy_pdf, "application/pdf")
        }

        response = self.client.post("/api/exams/upload", data=data, files=files)
        self.assertEqual(response.status_code, 201)
        res = response.json()
        self.assertTrue(res["success"])
        self.assertEqual(res["storage_status"], "LOCAL_ONLY")
        self.assertIsNone(res["file_url"])

    def test_delete_exam_atomically_success(self):
        """Valida exclusão em cascata (LGPD Art. 18) e remoção de arquivo se presente."""
        exam_uuid = uuid.uuid4()
        self.conn.fetchrow.return_value = {
            "id": exam_uuid,
            "user_id": self.user_id,
            "file_url": f"https://api.bragasaude.online/api/files/exams/{self.user_id}/{exam_uuid}.pdf"
        }

        with patch("os.path.exists", return_value=True), patch("os.remove") as mock_remove:
            response = self.client.delete(f"/api/exams/{exam_uuid}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["exam_id"], str(exam_uuid))
            mock_remove.assert_called_once()

        self.conn.execute.assert_called_once_with("DELETE FROM exams WHERE id = $1", exam_uuid)

    def test_delete_exam_unauthorized_if_different_user(self):
        """Impede que um usuário delete o exame pertencente a outro paciente."""
        exam_uuid = uuid.uuid4()
        self.conn.fetchrow.return_value = {
            "id": exam_uuid,
            "user_id": "other_patient_999",
            "file_url": None
        }

        response = self.client.delete(f"/api/exams/{exam_uuid}")
        self.assertEqual(response.status_code, 403)

    def test_get_user_exams_aggregates_items(self):
        """Valida que a listagem agrupa os itens laboratoriais dentro do exame correspondente."""
        exam_uuid = uuid.uuid4()
        item_uuid = uuid.uuid4()

        self.conn.fetch.side_effect = [
            # Retorno de exams
            [
                {
                    "id": exam_uuid,
                    "user_id": self.user_id,
                    "title": "Perfil Lipídico",
                    "category": "Laboratorial",
                    "exam_date": date(2026, 9, 15),
                    "file_url": None,
                    "status": "confirmed",
                    "created_at": datetime.now(timezone.utc)
                }
            ],
            # Retorno de exam_items
            [
                {
                    "id": item_uuid,
                    "exam_id": exam_uuid,
                    "user_id": self.user_id,
                    "item_key": "total_cholesterol",
                    "item_name": "Colesterol Total",
                    "value_numeric": 185.0,
                    "value_text": "185",
                    "unit": "mg/dL",
                    "measured_at": datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc)
                }
            ]
        ]

        response = self.client.get(f"/api/exams/{self.user_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(exam_uuid))
        self.assertEqual(len(data[0]["items"]), 1)
        self.assertEqual(data[0]["items"][0]["item_key"], "total_cholesterol")


if __name__ == "__main__":
    unittest.main()
