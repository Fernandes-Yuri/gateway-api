import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from routers import catalog
from api_identity import firebase_uid
from services.grocery_contributions import GroceryPriceContribution, AVERAGES_SQL, UPSERT_SQL
TODAY = datetime.now(ZoneInfo("America/Sao_Paulo")).date()

def payload():
    return dict(food_name="Feijão Carioca Cozido", amount_paid="8.00", quantity="500", unit="g", purchase_date=str(TODAY))

class ContributionValidationTests(unittest.TestCase):
    def test_normalizes_purchase_units(self):
        data = payload()
        self.assertEqual(GroceryPriceContribution(**data).normalized(), ("kg", Decimal("16")))
        data.update(unit="ml")
        self.assertEqual(GroceryPriceContribution(**data).normalized(), ("L", Decimal("16")))
        data.update(unit="un", quantity="12", amount_paid="12")
        self.assertEqual(GroceryPriceContribution(**data).normalized(), ("un", Decimal("1")))

    def test_rejects_invalid_values_dates_unknown_food_and_fractional_units(self):
        for key, value in [("amount_paid", "0"), ("amount_paid", "NaN"), ("amount_paid", "1.001"),
            ("quantity", "0"), ("quantity", "Infinity"), ("unit", "pacote"), ("food_name", "inexistente"),
            ("state", "ZZ"), ("purchase_date", str(TODAY + timedelta(days=1))),
            ("purchase_date", str(TODAY - timedelta(days=91)))]:
            data = payload(); data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValidationError, ValueError)):
                GroceryPriceContribution(**data).normalized()
        data = payload(); data.update(unit="un", quantity="1.5")
        with self.assertRaises(ValueError): GroceryPriceContribution(**data).normalized()

class ContributionRouteTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.conn.fetchval.side_effect = [True, 0]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        catalog.configure_state(db_pool=pool)
        self.addCleanup(catalog.configure_state)
        self.app = FastAPI(); self.app.include_router(catalog.router)
        self.app.dependency_overrides[firebase_uid] = lambda: "verified-user"
        self.client = TestClient(self.app)

    def test_verified_identity_and_cache_preserved(self):
        response = self.client.post("/api/catalog/grocery-price-contributions", json=payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["price_per_unit"], 16)
        args = self.conn.execute.call_args.args
        self.assertEqual(args[1], "verified-user")
        self.assertEqual(args[6:8], ("kg", Decimal("16")))
        self.assertNotIn("UPDATE grocery_price_cache", args[0])

    def test_authentication_and_daily_limit(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.post("/api/catalog/grocery-price-contributions", json=payload()).status_code, 401)
        self.app.dependency_overrides[firebase_uid] = lambda: "verified-user"
        self.conn.fetchval.side_effect = [True, 30]
        self.assertEqual(self.client.post("/api/catalog/grocery-price-contributions", json=payload()).status_code, 429)
        self.assertEqual(self.conn.execute.call_count, 1)

    def test_public_aggregates_hide_identity(self):
        self.conn.fetch.return_value = [dict(food_name="Feijão Carioca Cozido", unit="kg", price_avg=Decimal("16"),
            price_min=Decimal("14"), price_max=Decimal("18"), contributors=5, states_count=2)]
        response = self.client.get("/api/catalog/grocery-price-contributions/averages")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("user_id", response.text)
        self.assertNotIn("verified-user", response.text)

@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "PostgreSQL efêmero disponível somente no CI")
class ContributionSqlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from urllib.parse import urlparse
        dsn = os.environ["TEST_POSTGRES_URL"]; parsed = urlparse(dsn)
        if parsed.hostname not in ("localhost", "127.0.0.1") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("Somente banco local de teste permitido")
        self.conn = await asyncpg.connect(dsn)
        await self.conn.execute("CREATE TEMP TABLE profiles(id VARCHAR(128) PRIMARY KEY); CREATE TEMP TABLE grocery_price_cache(food_name TEXT PRIMARY KEY)")
        sql = (Path(__file__).resolve().parents[2] / "migrations/037_grocery_price_contributions.sql").read_text()
        await self.conn.execute(sql.replace("CREATE TABLE IF NOT EXISTS grocery_price_contributions", "CREATE TEMP TABLE IF NOT EXISTS grocery_price_contributions"))
        await self.conn.execute("INSERT INTO grocery_price_cache VALUES ('Feijão Carioca Cozido')")
        await self.conn.executemany("INSERT INTO profiles VALUES ($1)", [(f"user-{i}",) for i in range(8)])

    async def asyncTearDown(self): await self.conn.close()

    async def report(self, user, price, unit="kg", day=TODAY):
        await self.conn.execute(UPSERT_SQL, user, "Feijão Carioca Cozido", Decimal(price), Decimal("1"), unit, unit, Decimal(price), "SP", day)

    async def test_distinct_people_minimum_latest_outliers_units_and_account_deletion(self):
        for i in range(4): await self.report(f"user-{i}", "10")
        self.assertEqual(await self.conn.fetch(AVERAGES_SQL), [])
        await self.report("user-4", "10")
        await self.report("user-0", "12")
        await self.report("user-0", "500", day=TODAY - timedelta(days=1))
        await self.report("user-5", "1000")
        await self.report("user-6", "10", unit="un")
        await self.report("user-7", "500", day=TODAY - timedelta(days=91))
        rows = await self.conn.fetch(AVERAGES_SQL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit"], "kg")
        self.assertEqual(rows[0]["contributors"], 5)
        self.assertAlmostEqual(float(rows[0]["price_avg"]), 10.4)
        await self.conn.execute("DELETE FROM profiles WHERE id='user-0'")
        self.assertEqual(await self.conn.fetchval("SELECT COUNT(*) FROM grocery_price_contributions WHERE user_id='user-0'"), 0)
