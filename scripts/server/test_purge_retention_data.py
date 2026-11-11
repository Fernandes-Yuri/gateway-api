"""Testes unitários para a rotina de expurgo LGPD de retenção de dados."""
import unittest
from unittest.mock import AsyncMock
from purge_retention_data import purge_family_messages, purge_security_telemetry, run_retention_purge


class TestRetentionPurge(unittest.IsolatedAsyncioTestCase):
    async def test_purge_family_messages_batches(self):
        conn = AsyncMock()
        # Primeiro batch remove 500, segundo remove 120 (termina loop)
        conn.execute.side_effect = ["DELETE 500", "DELETE 120"]

        total = await purge_family_messages(conn, batch_size=500, max_batches=5)
        self.assertEqual(total, 620)
        self.assertEqual(conn.execute.call_count, 2)

    async def test_purge_security_telemetry(self):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 42"

        total = await purge_security_telemetry(conn, batch_size=1000)
        self.assertEqual(total, 42)
        conn.execute.assert_called_once()

    async def test_run_retention_purge_summary(self):
        conn = AsyncMock()
        conn.execute.side_effect = ["DELETE 10", "DELETE 25"]

        summary = await run_retention_purge(conn)
        self.assertEqual(
            summary,
            {
                "family_messages_deleted": 10,
                "security_telemetry_deleted": 25,
            },
        )
