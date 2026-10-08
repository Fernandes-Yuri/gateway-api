import unittest
from pydantic import ValidationError
from services.grocery_physical_metadata import IngredientPhysicalMetadata, FoodComponentMetadata
from test_nutrition_catalog_postgres import NutritionCatalogPostgres, ROOT
from services.ingredient_catalog import nutrition_snapshot
from services.grocery_contributions import AVERAGES_SQL


class PhysicalMetadataTests(unittest.TestCase):
    def test_optional_fields_preserve_previous_contract(self):
        self.assertIsNone(IngredientPhysicalMetadata.model_validate({}).grams_per_unit)
        self.assertIsNone(FoodComponentMetadata.model_validate({"shoppingComponents": ["a"]}).component_proportions)

    def test_positive_values_and_canonical_group(self):
        model = IngredientPhysicalMetadata(grams_per_unit=50, density_g_per_ml=0.92, canonical_group_slug="grupo")
        self.assertEqual(model.grams_per_unit, 50)
        self.assertEqual(model.density_g_per_ml, 0.92)
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValidationError): IngredientPhysicalMetadata(grams_per_unit=value)
            with self.assertRaises(ValidationError): IngredientPhysicalMetadata(density_g_per_ml=value)
        with self.assertRaises(ValidationError): IngredientPhysicalMetadata(canonical_group_slug=" ")

    def test_proportions_match_components_and_sum_to_one(self):
        model = FoodComponentMetadata(shoppingComponents=["a", "b"], component_proportions={"a": 0.8, "b": 0.2})
        self.assertEqual(model.component_proportions["a"], 0.8)
        for values in ({"a": 1}, {"a": 0.2, "b": 0.2}, {"a": 1.1, "b": -0.1}, {"a": float("nan"), "b": 0.2}):
            with self.assertRaises(ValidationError): FoodComponentMetadata(shoppingComponents=["a", "b"], component_proportions=values)


class PhysicalCatalogPostgres(NutritionCatalogPostgres):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / "migrations/041_grocery_ingredients_physical_metadata.sql").read_text())

    async def test_metadata_migration_is_idempotent_and_preserves_custom_values(self):
        snapshot = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(snapshot["version"], 3)
        self.assertEqual(len(snapshot["foods"]), 220)
        ingredients = {i["slug"]: i for i in snapshot["ingredients"]}
        self.assertEqual(ingredients["ovo-caipira"]["grams_per_unit"], 50)
        self.assertEqual(ingredients["azeite-de-oliva-extravirgem"]["density_g_per_ml"], 0.92)
        self.assertEqual(ingredients["leite-desnatado-pasteurizado"]["density_g_per_ml"], 1.03)
        self.assertTrue(all(f["component_proportions"] is None for f in snapshot["foods"]))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE grocery_ingredients SET details=details || '{\"grams_per_unit\":60}'::jsonb WHERE slug='ovo-caipira'")
            await conn.execute("UPDATE nutrition_catalog_settings SET revision=4 WHERE id=1")
            await conn.execute((ROOT / "migrations/041_grocery_ingredients_physical_metadata.sql").read_text())
        updated = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(updated["version"], 4)
        self.assertEqual(next(i for i in updated["ingredients"] if i["slug"] == "ovo-caipira")["grams_per_unit"], 60)

    async def test_database_enforces_physical_values_and_proportions(self):
        import asyncpg
        async with self.pool.acquire() as conn:
            for invalid in ('{"grams_per_unit":0}', '{"density_g_per_ml":"incorreto"}', '{"canonical_group_slug":" "}'):
                with self.assertRaises(asyncpg.CheckViolationError):
                    await conn.execute("UPDATE grocery_ingredients SET details=details || $1::jsonb WHERE slug='ovo-caipira'", invalid)
            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE food_catalog SET details=details || '{\"component_proportions\":{\"inexistente\":1}}'::jsonb WHERE remote_id='catalog_pts'")
            await conn.execute("UPDATE food_catalog SET details=details || '{\"component_proportions\":{\"proteina-texturizada-de-soja\":1}}'::jsonb WHERE remote_id='catalog_pts'")
        snapshot = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(next(f for f in snapshot["foods"] if f["remoteId"] == "catalog_pts")["component_proportions"], {"proteina-texturizada-de-soja": 1})
