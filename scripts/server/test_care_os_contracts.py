from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from schemas.medications import BleTelemetryRequest, MedicationCreate
from services.care_os import new_public_token, require_patient_permission, token_hash


class CareOsContractTests(unittest.TestCase):
    def test_public_tokens_are_only_persisted_as_sha256(self):
        token, digest = new_public_token()
        self.assertNotEqual(token, digest)
        self.assertEqual(token_hash(token), digest)
        self.assertEqual(len(digest), 64)

    def test_medication_contract_normalizes_times_and_requires_confirmation(self):
        model = MedicationCreate(
            name="Losartana", schedule_times=["08:00", "20:00"], total_units=60,
            confirmed_with_prescription=True,
        )
        self.assertEqual(len(model.schedule_times), 2)

    def test_ble_contract_accepts_gatt_measurement(self):
        model = BleTelemetryRequest(
            patient_id="patient", device_id="omron-001", device_type="BLOOD_PRESSURE",
            measured_at=datetime.now(timezone.utc), systolic_pressure=120, diastolic_pressure=80,
        )
        self.assertEqual(model.protocol, "GATT")


class CareOsPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_viewer_is_read_only_without_explicit_permission(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"caregiver_role": "CAREGIVER_VIEWER", "permissions": []}
        with self.assertRaises(HTTPException) as error:
            await require_patient_permission(conn, "caregiver", "patient", "medication:take")
        self.assertEqual(error.exception.status_code, 403)

    async def test_explicit_permission_can_extend_viewer_role(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"caregiver_role": "CAREGIVER_VIEWER", "permissions": ["medication:take"]}
        result = await require_patient_permission(conn, "caregiver", "patient", "medication:take")
        self.assertEqual(result["role"], "CAREGIVER_VIEWER")
