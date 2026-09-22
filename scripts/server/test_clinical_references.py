import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from clinical_references import install_clinical_reference_routes


class ClinicalReferenceApiTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetch.return_value = [{
            "item_key": "glucose_fasting",
            "item_name": "Glicose em Jejum",
            "category": "Metabólico",
            "unit": "mg/dL",
            "min_target": 70.0,
            "max_target": 99.0,
            "min_critical": 54.0,
            "max_critical": 300.0,
            "interpretation_hint": "Jejum de 8 a 12 horas.",
            "institution": "SBD (Sociedade Brasileira de Diabetes) / ADA",
            "document_version": "2026",
            "parameter": "Glicemia Basal",
            "updated_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
        }]
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.app = FastAPI()
        install_clinical_reference_routes(self.app, lambda: self.pool)
        self.app.dependency_overrides[firebase_uid] = lambda: "user-1"
        self.client = TestClient(self.app)

    def test_lists_references_with_source_traceability(self):
        response = self.client.get(
            "/api/clinical/references",
            params={"category": "Metabólico", "item_key": "glucose_fasting"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["references"][0]["institution"], "SBD (Sociedade Brasileira de Diabetes) / ADA")
        self.assertEqual(body["references"][0]["document_version"], "2026")
        query, category, item_key = self.conn.fetch.await_args.args
        self.assertIn("category = $1", query)
        self.assertIn("item_key = $2", query)
        self.assertEqual((category, item_key), ("Metabólico", "glucose_fasting"))

    def test_database_unavailable_is_503(self):
        app = FastAPI()
        install_clinical_reference_routes(app, lambda: None)
        app.dependency_overrides[firebase_uid] = lambda: "user-1"
        response = TestClient(app).get("/api/clinical/references")
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
