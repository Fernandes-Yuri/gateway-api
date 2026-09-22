import asyncio
import unittest

from fastapi import HTTPException, status

from routers import sync as sync_router


class LeagueDisabledTests(unittest.TestCase):
    def test_membership_sync_is_gone_without_touching_database(self):
        payload = sync_router.LeagueMembershipSyncDto(
            id="00000000-0000-0000-0000-000000000001",
            userId="owner-1",
            userName="Pessoa",
            userLevel=1,
            userStreak=0,
            leagueCycleId="00000000-0000-0000-0000-000000000002",
            xpEarned=10,
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(sync_router.sync_league_membership(payload, uid="owner-1"))

        self.assertEqual(status.HTTP_410_GONE, raised.exception.status_code)
        self.assertEqual(
            "LEAGUE_FEATURE_DISABLED",
            raised.exception.detail["code"],
        )


if __name__ == "__main__":
    unittest.main()
