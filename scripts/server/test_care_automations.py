import unittest
from unittest.mock import AsyncMock

from services.care_automations import _deliver_or_release


class CareAutomationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_whatsapp_delivery_releases_dedupe_claim(self):
        conn = AsyncMock()
        sender = AsyncMock(return_value=False)
        delivered = await _deliver_or_release(
            conn, sender, "claim-1", "5511999999999", "Resumo", "NIGHTLY_BULLETIN",
        )
        self.assertFalse(delivered)
        conn.execute.assert_awaited_once_with(
            "DELETE FROM care_notification_deliveries WHERE id=$1", "claim-1",
        )

    async def test_successful_delivery_keeps_dedupe_claim(self):
        conn = AsyncMock()
        sender = AsyncMock(return_value=True)
        delivered = await _deliver_or_release(
            conn, sender, "claim-1", "5511999999999", "Resumo", "NIGHTLY_BULLETIN",
        )
        self.assertTrue(delivered)
        conn.execute.assert_awaited_once_with(
            "UPDATE care_notification_deliveries SET delivered_at=now() WHERE id=$1", "claim-1",
        )
