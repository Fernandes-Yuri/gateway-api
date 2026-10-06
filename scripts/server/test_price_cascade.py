"""Contratos do job IPCA e da importação manual em lote."""
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import catalog
from services import price_cascade as prices


def history(price=10, source="meli", updated=None):
    return {"price_avg": price, "price_min": price, "price_max": price, "source": source,
            "updated_at": updated or datetime(2026, 1, 1, tzinfo=timezone.utc)}


class EstimateTests(unittest.TestCase):
    def test_no_history_no_estimate(self):
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.assertIsNone(prices._estimate_ipca(None, now, {}))
        self.assertIsNone(prices._estimate_ipca(history(None), now, {}))

    def test_full_month_and_compound(self):
        result = prices._estimate_ipca(history(), datetime(2026, 3, 1, tzinfo=timezone.utc),
                                      {"202601": .1, "202602": .2})
        self.assertEqual(result["avg"], Decimal("13.20"))
        self.assertEqual(result["source"], "estimated")
        self.assertEqual(result["count"], 0)

    def test_weekly_fraction_and_deflation(self):
        now = datetime(2026, 1, 8, tzinfo=timezone.utc)
        result = prices._estimate_ipca(history(100), now, {"202601": .01})
        self.assertEqual(result["avg"], Decimal("100.22"))
        result = prices._estimate_ipca(history(100), datetime(2026, 2, 1, tzinfo=timezone.utc), {"202601": -.01})
        self.assertEqual(result["avg"], Decimal("99.00"))

    @patch.dict(os.environ, {"IPCA_MONTHLY_FALLBACK": "0.003"})
    def test_fallback_and_same_instant(self):
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(prices._estimate_ipca(history(100), now, {})["avg"], Decimal("100.30"))
        self.assertEqual(prices._estimate_ipca(history(100, updated=now), now, {})["avg"], Decimal("100.00"))

    @patch.dict(os.environ, {"IPCA_MONTHLY_FALLBACK": "nan"})
    def test_invalid_fallback(self):
        with self.assertRaises(ValueError):
            prices._estimate_ipca(history(), datetime(2026, 2, 1, tzinfo=timezone.utc), {})

    def test_invalid_timestamp_preserves_history_by_failing(self):
        previous = history()
        previous["updated_at"] = None
        with self.assertRaises(ValueError):
            prices._estimate_ipca(previous, datetime(2026, 2, 1, tzinfo=timezone.utc), {})
        with self.assertRaises(ValueError):
            prices._estimate_ipca(history(updated=datetime(2027, 1, 1, tzinfo=timezone.utc)),
                                  datetime(2026, 2, 1, tzinfo=timezone.utc), {})


class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        baseline_patch = patch.object(prices, "fetch_baseline", new=AsyncMock(return_value=({}, 0)))
        baseline_patch.start()
        self.addCleanup(baseline_patch.stop)

    async def test_sidra_and_http_failure(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json=[{}, {"D3C": "202601", "V": "0.30"}]))) as client:
            rates, errors = await prices._fetch_ipca(client)
        self.assertEqual(rates, {"202601": .003})
        self.assertEqual(errors, 0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            self.assertEqual(await prices._fetch_ipca(client), ({}, 1))

    async def test_manual_skipped_missing_marked_and_history_estimated(self):
        pool = AsyncMock()
        items = [{"food_name": n, "search_term": n} for n in ("manual", "antigo", "novo")]
        pool.fetch.return_value = [dict(history(source="manual"), food_name="manual"),
                                   dict(history(), food_name="antigo")]
        pool.fetchrow.return_value = {"food_name": "salvo"}
        with patch.object(prices._SEED_PATH.__class__, "read_text", return_value=json.dumps(items)), \
                patch.object(prices, "_fetch_ipca", new=AsyncMock(return_value=({}, 0))), \
                patch.object(prices.asyncio, "sleep", new=AsyncMock()):
            result = await prices.atualizar_precos(pool)
        self.assertEqual(result, {"atualizados": 1, "sem_preco": 1, "erros": 0})
        calls = pool.fetchrow.call_args_list
        self.assertEqual(calls[0].args[6], "estimated")
        self.assertEqual(calls[1].args[3], None)
        self.assertTrue(calls[1].args[8])
        self.assertIn("WHERE grocery_price_cache.source <> 'manual'", calls[0].args[0])
        self.assertIn("IS NOT DISTINCT FROM $9", calls[0].args[0])

    async def test_seed_coverage_requires_baseline(self):
        items = json.loads(prices._SEED_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(items), 220)
        pool = AsyncMock()
        pool.fetch.return_value = [dict(history(), food_name=r["food_name"]) for r in items]
        pool.fetchrow.return_value = {"food_name": "salvo"}
        # MELI bloqueado não recebe nenhuma chamada: só SIDRA é usado.
        requests = []
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(403)
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(prices.httpx, "AsyncClient", return_value=client), \
                patch.object(prices.asyncio, "sleep", new=AsyncMock()):
            result = await prices.atualizar_precos(pool)
        self.assertEqual(result, {"atualizados": 220, "sem_preco": 0, "erros": 1})
        self.assertEqual(len(requests), 1)
        self.assertIn("apisidra.ibge.gov.br", requests[0])

    async def test_item_failure_continues_and_race_not_counted(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        pool.fetchrow.side_effect = [RuntimeError("db"), None, {"food_name": "ok"}]
        items = [{"food_name": str(n), "search_term": str(n)} for n in range(3)]
        with patch.object(prices._SEED_PATH.__class__, "read_text", return_value=json.dumps(items)), \
                patch.object(prices, "_fetch_ipca", new=AsyncMock(return_value=({}, 0))), \
                patch.object(prices.asyncio, "sleep", new=AsyncMock()):
            result = await prices.atualizar_precos(pool)
        self.assertEqual(result, {"atualizados": 0, "sem_preco": 1, "erros": 1})


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        catalog.configure_state(db_pool=self.pool)
        self.addCleanup(catalog.configure_state)
        app = FastAPI()
        app.include_router(catalog.router)
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {"SERVICE_TOKEN": "test-only-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.headers = {"Authorization": "Bearer test-only-token"}
        self.url = "/api/admin/grocery-prices/import-csv"

    def test_json_validates_and_inserts_unknown_food(self):
        response = self.client.post(self.url, headers=self.headers, json=[
            {"food_name": "novo", "price": 12.34}, {"food_name": "zero", "price": 0},
            {"food_name": "negativo", "price": -1}, {"food_name": "muito", "price": 100000000},
            {"food_name": "fracao", "price": 1.001}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["importados"], 1)
        self.assertEqual(response.json()["erros"], 4)
        sql, rows = self.conn.executemany.call_args.args
        self.assertIn("ON CONFLICT", sql)
        self.assertEqual(rows, [("novo", Decimal("12.34"))])

    def test_csv_220_and_malformed_row(self):
        items = json.loads(prices._SEED_PATH.read_text(encoding="utf-8"))
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["food_name", "price"])
        writer.writerows((r["food_name"], "10.00") for r in items)
        writer.writerow(["erro", "abc"])
        response = self.client.post(self.url, headers=self.headers, content=output.getvalue().encode())
        self.assertEqual(response.json()["importados"], 220)
        self.assertEqual(response.json()["erros"], 1)

    def test_bad_document_auth_and_duplicates(self):
        self.assertEqual(self.client.post(self.url, content="invalid").status_code, 401)
        self.assertEqual(self.client.post(self.url, headers=self.headers, content="invalid").status_code, 422)
        response = self.client.post(self.url, headers=self.headers, json=[
            {"food_name": "a", "price": 1}, {"food_name": "a", "price": 2},
            {"food_name": "b", "price": True}])
        self.assertEqual(response.json()["erros"], 2)

    def test_extra_csv_column_does_not_reject_valid_rows(self):
        response = self.client.post(self.url, headers=self.headers,
                                    content=b"food_name,price\na,1.00\nb,2.00,extra\nc,3.00\n")
        self.assertEqual(response.json()["importados"], 2)
        self.assertEqual(response.json()["erros"], 1)

    def test_database_failure_rolls_back_and_hides_details(self):
        self.conn.executemany.side_effect = RuntimeError("segredo")
        response = self.client.post(self.url, headers=self.headers, json=[{"food_name": "novo", "price": 1}])
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("segredo", response.text)
        self.conn.transaction.return_value.__aexit__.assert_awaited_once()

    def test_baseline_preserves_manual_and_enables_estimates(self):
        self.conn.fetchrow.side_effect = [{"food_name": "novo"}, None]
        response = self.client.post(self.url + "?mode=baseline", headers=self.headers, json=[
            {"food_name": "novo", "price": 10}, {"food_name": "manual", "price": 20}])
        self.assertEqual(response.json()["importados"], 1)
        self.assertEqual(response.json()["erros"], 1)
        sql = self.conn.fetchrow.call_args_list[0].args[0]
        self.assertIn("'estimated'", sql)
        self.assertIn("WHERE grocery_price_cache.source <> 'manual'", sql)


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "PostgreSQL efêmero disponível somente no CI")
class PriceSqlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from urllib.parse import urlparse
        dsn = os.environ["TEST_POSTGRES_URL"]
        parsed = urlparse(dsn)
        if parsed.hostname not in ("localhost", "127.0.0.1") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("Somente banco local de teste permitido")
        self.conn = await asyncpg.connect(dsn)
        await self.conn.execute("""CREATE TEMP TABLE grocery_price_cache (
            food_name TEXT PRIMARY KEY, search_term TEXT NOT NULL, price_avg NUMERIC(10,2),
            price_min NUMERIC(10,2), price_max NUMERIC(10,2), source TEXT NOT NULL,
            results_count INT, updated_at TIMESTAMPTZ DEFAULT now(), needs_manual BOOLEAN)""")

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_real_sql_manual_race_source_and_timestamp(self):
        await self.conn.execute(catalog._MANUAL_UPSERT, "manual", Decimal("10.00"))
        before = await self.conn.fetchrow("SELECT * FROM grocery_price_cache WHERE food_name='manual'")
        result = await self.conn.fetchrow(prices._UPSERT, "manual", "termo", Decimal("99"),
                                         Decimal("99"), Decimal("99"), "estimated", 0, False, before["updated_at"])
        self.assertIsNone(result)
        self.assertIsNone(await self.conn.fetchrow(catalog._BASELINE_UPSERT, "manual", Decimal("30")))
        after = await self.conn.fetchrow("SELECT * FROM grocery_price_cache WHERE food_name='manual'")
        self.assertEqual(dict(before), dict(after))
        await self.conn.fetchrow(catalog._BASELINE_UPSERT, "antigo", Decimal("12"))
        previous = await self.conn.fetchrow("SELECT * FROM grocery_price_cache WHERE food_name='antigo'")
        result = await self.conn.fetchrow(prices._UPSERT, "antigo", "termo", Decimal("13"),
                                         Decimal("13"), Decimal("13"), "estimated", 0, False, previous["updated_at"])
        self.assertIsNotNone(result)
        # Um snapshot antigo não pode sobrescrever uma atualização concorrente.
        result = await self.conn.fetchrow(prices._UPSERT, "antigo", "termo", Decimal("14"),
                                         Decimal("14"), Decimal("14"), "estimated", 0, False, previous["updated_at"])
        self.assertIsNone(result)
        self.assertEqual(await self.conn.fetchval("SELECT price_avg FROM grocery_price_cache WHERE food_name='antigo'"), Decimal("13"))


if __name__ == "__main__":
    unittest.main()
