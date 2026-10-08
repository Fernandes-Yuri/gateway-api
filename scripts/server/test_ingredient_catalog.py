import unittest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from services.ingredient_catalog import key, prices_for_ingredients, nutrition_snapshot, resolve_ingredient


def pool_for(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def ingredient(slug="test", name="Ingrediente", unit="kg"):
    return dict(slug=slug, name=name, unit=unit, details={"aliases": ["Preparo de teste"], "food_ids": ["remote"], "step": 500, "minimum": 500})


class IngredientCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_database_ingredients_and_does_not_relabel_old_prices(self):
        conn = AsyncMock(); pool = pool_for(conn)
        old = dict(food_name="Ingrediente aposentado", price_avg=Decimal("45"), price_unit="kg", source="manual", updated_at=datetime.now(timezone.utc))
        conn.fetch.side_effect = [[ingredient(name="Ingrediente novo")], [old], []]
        rows = await prices_for_ingredients(pool, "averages")
        self.assertEqual(rows[0]["name"], "Ingrediente novo")
        self.assertIsNone(rows[0]["price_avg"])
        self.assertEqual(rows[0]["source"], "unavailable")
        self.assertIn("WHERE active", conn.fetch.call_args_list[0].args[0])

    async def test_prefers_matching_community_unit_and_explicit_manual_price(self):
        conn = AsyncMock(); pool = pool_for(conn)
        def historical(source="estimated", unit="kg"):
            return dict(food_name="Ingrediente", price_avg=Decimal("10"), source=source, price_unit=unit, updated_at=datetime.now(timezone.utc))
        community = [dict(food_name="Ingrediente", unit="kg", price_avg=Decimal("16"), contributors=5)]
        for source, unit, community_rows, expected in [("estimated", "kg", community, (16, "community")), ("manual", "kg", community, (10, "manual")), ("estimated", None, [], (None, "unavailable"))]:
            conn.fetch.side_effect = [[ingredient()], [historical(source, unit)], community_rows]
            row = (await prices_for_ingredients(pool, "averages"))[0]
            self.assertEqual((row["price_avg"], row["source"]), expected)

    async def test_snapshot_reads_consistent_database_revision_and_mapping(self):
        conn = AsyncMock(); pool = pool_for(conn)
        conn.fetchrow.return_value = {"revision": 7, "policy": '{"required_groups":[["remote"]]}'}
        conn.fetch.side_effect = [[{"food": '{"remoteId":"remote","shoppingComponents":["test"]}'}], [ingredient()], [], []]
        snapshot = await nutrition_snapshot(pool, "averages")
        self.assertEqual(snapshot["version"], 7)
        self.assertEqual(snapshot["foods"][0]["shoppingComponents"], ["test"])
        conn.transaction.assert_called_once_with(readonly=True, isolation="repeatable_read")

    async def test_contributions_resolve_active_database_aliases(self):
        conn = AsyncMock(); conn.fetch.return_value = [ingredient()]
        self.assertEqual((await resolve_ingredient(conn, "preparo DE teste"))["slug"], "test")
        with self.assertRaises(ValueError): await resolve_ingredient(conn, "fora do catálogo")
        self.assertEqual(key("FEIJÃO"), "feijao")
