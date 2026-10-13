import unittest

from services.health_queries import blood_pressure_today
from services.operations import OperationContext


class FakeConnection:
    def __init__(self):
        self.query = None
        self.args = None

    async def fetchval(self, query, *args):
        return None

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        return None


class HealthQueryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pressure_today_filters_to_patient_local_day(self):
        conn = FakeConnection()
        result = await blood_pressure_today(
            conn, OperationContext(actor_id="patient", patient_id="patient"))
        self.assertEqual(result.status, "missing_data")
        self.assertIn("para você hoje", result.message)
        self.assertIn("measured_at >= $2 AND measured_at < $3", conn.query)
        self.assertEqual(conn.args[0], "patient")
        self.assertEqual(len(conn.args), 3)
        self.assertLess(conn.args[1], conn.args[2])


if __name__ == "__main__":
    unittest.main()
