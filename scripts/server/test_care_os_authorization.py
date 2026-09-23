"""Cobertura Zero-Trust dos contratos familiares: 200, 401 e 403."""
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import care_os


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def app_with(conn, uid=None):
    app = FastAPI()
    care_os.configure_state(db_pool=Pool(conn), public_base_url="https://test.invalid")
    app.include_router(care_os.router)
    if uid is not None:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app)


class CareOsAuthorizationTests(unittest.TestCase):
    def test_family_stock_200_for_patient_owner(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[{
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "Losartana", "schedule_times": "08:00", "schedule_time": "08:00",
            "current_units": 5, "alert_threshold_days": 5,
        }])
        response = app_with(conn, "patient-1").get("/api/family/patients/patient-1/medications/stock")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["days_remaining"], 5)
        self.assertTrue(response.json()[0]["is_critical"])

    def test_family_stock_401_without_bearer_token(self):
        conn = MagicMock()
        response = app_with(conn).get("/api/family/patients/patient-1/medications/stock")
        self.assertEqual(response.status_code, 401)

    def test_family_stock_403_for_unbound_caregiver(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        response = app_with(conn, "intruder").get("/api/family/patients/patient-1/medications/stock")
        self.assertEqual(response.status_code, 403)
        conn.fetch.assert_not_awaited()

    def test_medication_requires_human_prescription_confirmation(self):
        conn = MagicMock()
        response = app_with(conn, "patient-1").post(
            "/api/family/patients/patient-1/medications",
            json={
                "name": "Losartana", "schedule_times": ["08:00:00"], "total_units": 30,
                "confirmed_with_prescription": False,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_medication_photo_rejects_unbound_caregiver(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        response = app_with(conn, "intruder").post(
            "/api/family/patients/patient-1/medications/photo",
            files={"file": ("caixa.jpg", b"image", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 403)

    def test_restock_rejects_reused_key_with_different_payload(self):
        medication_id = "00000000-0000-0000-0000-000000000001"
        conn = MagicMock()
        conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": medication_id, "user_id": "patient-1", "name": "Losartana"},
            {
                "id": "00000000-0000-0000-0000-000000000010",
                "medication_id": "00000000-0000-0000-0000-000000000002",
                "new_total_units": 60,
            },
        ])
        response = app_with(conn, "patient-1").post(
            f"/api/medications/{medication_id}/restock",
            json={"new_total_units": 30, "idempotency_key": "restock-12345678"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_new_gateway_routes_are_present_in_openapi(self):
        paths = app_with(MagicMock(), "patient-1").get("/openapi.json").json()["paths"]
        self.assertIn("/api/medications/{medication_id}/restock", paths)
        self.assertIn("/api/family/patients/{patient_id}/medications/photo", paths)
        self.assertIn("/api/files/medications/{patient_id}/{filename}", paths)
