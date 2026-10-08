"""Migrações e contratos do catálogo em PostgreSQL efêmero, nunca no RDS."""
import os
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from services.ingredient_catalog import nutrition_snapshot, resolve_ingredient
from services.grocery_contributions import AVERAGES_SQL, GroceryPriceContribution
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DSN = os.getenv("TEST_POSTGRES_URL")


@unittest.skipUnless(DSN, "PostgreSQL efêmero disponível no CI")
class NutritionCatalogPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(DSN)
        if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("Somente banco efêmero local braga_privacy_test")
        self.schema = "catalog_" + uuid.uuid4().hex
        self.control = await asyncpg.connect(DSN)
        await self.control.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        await self.control.execute(f'CREATE SCHEMA "{self.schema}"')
        async def setup(conn):
            await conn.execute(f'SET search_path TO "{self.schema}", public')
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, setup=setup)
        async with self.pool.acquire() as conn:
            await conn.execute((ROOT / "scripts/docker/init_schema.sql").read_text())
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,name TEXT)")
            for number in (26, 37, 38):
                await conn.execute(next(p for p in (ROOT / "migrations").glob(f"{number:03d}_*.sql") if not p.stem.endswith("_rollback")).read_text())
            await conn.execute("INSERT INTO grocery_price_cache(food_name,search_term,price_avg,source,price_unit) VALUES ('Soja Verde em Grãos (Edamame)','edamame',45,'manual','kg'),('PTS','pts',NULL,'unavailable','kg')")
            await conn.execute("INSERT INTO profiles(id,full_name) VALUES ('p','Teste')")
            await conn.execute("INSERT INTO grocery_price_contributions(user_id,food_name,amount_paid,quantity,original_unit,normalized_unit,normalized_price,purchase_date,ingredient_slug) VALUES ('p','Soja Verde em Grãos (Edamame)',45,1,'kg','kg',45,CURRENT_DATE,'soja-verde-em-graos-edamame')")
            await conn.execute((ROOT / "migrations/040_nutrition_catalog_rds.sql").read_text())

    async def asyncTearDown(self):
        await self.pool.close()
        await self.control.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.control.close()

    async def test_replaces_two_slots_preserves_history_and_reads_live_database(self):
        snapshot = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(len(snapshot["foods"]), 220)
        ids = {f["remoteId"] for f in snapshot["foods"]}
        self.assertTrue({"catalog_arroz_branco_tipo_1", "catalog_pts", "food_121", "food_122", "food_132"} <= ids)
        self.assertFalse({"food_130", "food_135"} & ids)
        pts = next(i for i in snapshot["ingredients"] if i["slug"] == "proteina-texturizada-de-soja")
        self.assertIsNone(pts["price_avg"])
        food = next(f for f in snapshot["foods"] if f["remoteId"] == "catalog_pts")
        self.assertEqual(food["shoppingComponents"], [pts["slug"]])
        self.assertEqual(food["proteinG"], 25.4)
        self.assertEqual(snapshot["policy"]["required_groups"][0][0], "catalog_arroz_branco_tipo_1")
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT ingredient_slug FROM grocery_price_contributions WHERE user_id='p'"), "soja-verde-em-graos-edamame")
            self.assertEqual(await conn.fetchval("SELECT price_avg FROM grocery_price_cache WHERE food_name='Soja Verde em Grãos (Edamame)'"), 45)
            await conn.execute("UPDATE food_catalog SET name='Nome atualizado no banco' WHERE remote_id='catalog_pts'")
            await conn.execute("UPDATE nutrition_catalog_settings SET revision=3 WHERE id=1")
            await conn.execute((ROOT / "migrations/040_nutrition_catalog_rds.sql").read_text())
        updated = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(updated["version"], 3)
        self.assertEqual(next(f["name"] for f in updated["foods"] if f["remoteId"] == "catalog_pts"), "Nome atualizado no banco")

    async def test_new_contributions_resolve_soy_alias_and_require_dry_mass_unit(self):
        async with self.pool.acquire() as conn:
            item = await resolve_ingredient(conn, "PTS")
            with self.assertRaises(ValueError): await resolve_ingredient(conn, "Soja Verde em Grãos (Edamame)")
        body = GroceryPriceContribution(food_name="PTS", amount_paid="12", quantity="400", unit="g", purchase_date=datetime.now(ZoneInfo("America/Sao_Paulo")).date())
        self.assertEqual(body.normalized(item)[0], "kg")
        self.assertEqual(body.normalized(item)[1], 30)
        with self.assertRaises(ValueError): body.model_copy(update={"unit": "un"}).normalized(item)

    async def test_compensation_preserves_new_contributions_and_restores_previous_slots(self):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO grocery_price_contributions(user_id,food_name,amount_paid,quantity,original_unit,normalized_unit,normalized_price,purchase_date,ingredient_slug) VALUES ('p','PTS',12,1,'kg','kg',12,CURRENT_DATE,'proteina-texturizada-de-soja')")
            await conn.execute((ROOT / "migrations/040_nutrition_catalog_rds_rollback.sql").read_text())
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM grocery_price_contributions"), 2)
        snapshot = await nutrition_snapshot(self.pool, AVERAGES_SQL)
        self.assertEqual(len(snapshot["foods"]), 220)
        self.assertEqual(snapshot["version"], 3)
        self.assertTrue({"food_130", "food_135"} <= {f["remoteId"] for f in snapshot["foods"]})
        self.assertTrue(all("catalog_pts" not in g for g in snapshot["policy"]["required_groups"]))

    async def test_rejects_occupied_legacy_table_without_partial_updates(self):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO food_catalog(name,kcal,carbs,proteins,fats,sodium) VALUES ('Legado',1,1,1,1,1)")
            with self.assertRaises(asyncpg.PostgresError):
                await conn.execute((ROOT / "migrations/040_nutrition_catalog_rds.sql").read_text())
            await conn.execute("ROLLBACK")
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM food_catalog WHERE remote_id IS NULL"), 1)
