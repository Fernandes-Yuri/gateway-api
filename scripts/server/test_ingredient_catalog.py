import unittest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from services.ingredient_catalog import INGREDIENTS, MANIFEST, ALIASES, key, prices_for_ingredients

class IngredientCatalogTests(unittest.IsolatedAsyncioTestCase):
    def test_mapping_covers_all_foods_and_preserves_real_product_variants(self):
        self.assertEqual(len(MANIFEST["foods"]), 221)
        self.assertEqual(len(INGREDIENTS), 219)
        for food in MANIFEST["foods"]:
            self.assertTrue(food["shopping_components"])
            self.assertTrue(all(slug in INGREDIENTS for slug in food["shopping_components"]))
        eggs = [f for f in MANIFEST["foods"] if f["food_name"].startswith("Ovo Caipira")]
        self.assertEqual({f["ingredient_slug"] for f in eggs}, {"ovo-caipira"})
        for slug in ["oleo-de-soja", "ovos", "pao-integral", "salsinha", "tapioca"]:
            self.assertIn(slug, INGREDIENTS)
        for names in [("Banana-Prata", "Banana-Maçã"), ("Quinoa em Flocos", "Quinoa em Grãos Cozida"),
                      ("Arroz Integral Agulhinha", "Arroz Negro Integral")]:
            self.assertNotEqual(ALIASES[key(names[0])]["slug"], ALIASES[key(names[1])]["slug"])

    async def test_prefers_matching_community_unit_and_explicit_manual_price(self):
        conn = AsyncMock(); pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        def historical(source="estimated", unit="kg"):
            return dict(food_name="Cenoura Crua Ralada", price_avg=Decimal("10"), price_min=Decimal("10"),
                price_max=Decimal("10"), source=source, price_unit=unit, updated_at=datetime.now(timezone.utc))
        community = [dict(food_name="Cenoura", unit="kg", price_avg=Decimal("16"), contributors=5)]
        conn.fetch.side_effect = [[historical()], community]
        rows = await prices_for_ingredients(pool, "query")
        row = next(x for x in rows if x["slug"] == "cenoura")
        self.assertEqual((row["price_avg"], row["source"]), (16, "community"))
        conn.fetch.side_effect = [[historical("manual")], community]
        row = next(x for x in await prices_for_ingredients(pool, "query") if x["slug"] == "cenoura")
        self.assertEqual((row["price_avg"], row["source"]), (10, "manual"))
        conn.fetch.side_effect = [[historical(unit=None)], []]
        row = next(x for x in await prices_for_ingredients(pool, "query") if x["slug"] == "cenoura")
        self.assertIsNone(row["price_avg"])
