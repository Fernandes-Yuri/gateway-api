import unittest
from datetime import datetime, timezone

from services.medication_queries import _period, medication_take_history
from services.operations import OperationContext


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetch_calls = []

    async def fetchval(self, query, *args):
        return None

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows


class MedicationHistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_period_parses_portuguese_accents_and_local_day(self):
        now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
        start, end, label = _period("hoje de manhã", now)
        self.assertEqual((start.hour, start.minute, end.hour), (5, 0, 12))
        self.assertEqual(label, "hoje pela manhã")

    async def test_missing_record_does_not_claim_missed_dose(self):
        conn = FakeConnection()
        result = await medication_take_history(
            conn, OperationContext(actor_id="owner", patient_id="owner"),
            "Ele tomou o remédio hoje de manhã?")
        self.assertEqual(result.status, "missing_data")
        self.assertIn("não encontrei registro", result.message.lower())
        self.assertIn("ausência de registro", result.message.lower())
        self.assertIn("não permite concluir", result.message.lower())
        self.assertNotIn("não tomou", result.message.lower())
        self.assertEqual(len(conn.fetch_calls), 1)
        self.assertEqual(conn.fetch_calls[0][1][0], "owner")

    async def test_found_record_reports_person_and_actual_time(self):
        conn = FakeConnection([{
            "name": "Losartana", "taken_at": datetime(2026, 10, 13, 8, 15, tzinfo=timezone.utc),
            "units_taken": 1,
        }])
        result = await medication_take_history(
            conn, OperationContext(actor_id="owner", patient_id="owner"),
            "Ele tomou o remédio hoje de manhã?")
        self.assertEqual(result.status, "success")
        self.assertIn("Losartana", result.message)
        self.assertIn("registrado", result.message)

    async def test_query_failure_is_not_reported_as_no_record(self):
        class BrokenConnection(FakeConnection):
            async def fetch(self, query, *args):
                raise RuntimeError("db down")

        result = await medication_take_history(
            BrokenConnection(), OperationContext(actor_id="owner", patient_id="owner"),
            "Ele tomou o remédio hoje de manhã?")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("não consegui consultar", result.message.lower())


if __name__ == "__main__":
    unittest.main()
