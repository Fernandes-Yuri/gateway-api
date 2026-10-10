"""Contratos do painel administrativo por ingrediente (fonte única do catálogo)."""
import json
import os
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import catalog
from services.ingredient_catalog import build_admin_rows


def ingredient(slug, name, unit, details=None, active=True):
    details = details or {}
    return {"slug": slug, "name": name, "unit": unit, "details": details, "active": active,
            "step": 1, "minimum": 50, "aliases": details.get("aliases", []),
            "food_ids": details.get("food_ids", []),
            "grams_per_unit": details.get("grams_per_unit"),
            "canonical_group_slug": details.get("canonical_group_slug")}


def legacy(name, price, unit, source="estimated", updated="2026-01-01T00:00:00+00:00"):
    return {"food_name": name, "price_avg": price, "price_min": price, "price_max": price,
            "source": source, "price_unit": unit, "updated_at": datetime.fromisoformat(updated)}


def community(name, unit, avg, contributors):
    return {"food_name": name, "unit": unit, "price_avg": avg, "price_min": avg, "price_max": avg,
            "contributors": contributors, "states_count": 2}


CATALOG = [
    ingredient("feijao-carioca", "Feijão Carioca", "kg", {"aliases": ["Feijão Carioca Cozido"], "food_ids": ["food_150"]}),
    ingredient("pao-frances", "Pão Francês Tradicional", "un", {"aliases": ["Pão Francês"], "canonical_group_slug": "pao"}),
    ingredient("agua-coco", "Água de Coco", "L"),
    ingredient("arroz-branco", "Arroz Branco", "kg", active=False),
]
CATEGORIES = [{"remote_id": "food_150", "category": "Leguminosas"}]


class AdminRowTests(unittest.TestCase):
    def test_priority_manual_over_community_over_estimated(self):
        rows = build_admin_rows(
            CATALOG,
            [legacy("Feijão Carioca Cozido", "9.90", "kg", "estimated"),
             legacy("Pão Francês", "12.00", "un", "estimated"),
             legacy("Água de Coco", "5.00", "L", "manual")],
            [community("Feijão Carioca", "kg", 10.5, 7)],
            CATEGORIES)
        by_slug = {row["slug"]: row for row in rows}
        self.assertEqual(by_slug["feijao-carioca"]["source"], "community")
        self.assertEqual(by_slug["feijao-carioca"]["price_avg"], 10.5)
        self.assertEqual(by_slug["feijao-carioca"]["contributors"], 7)
        self.assertEqual(by_slug["pao-frances"]["source"], "estimated")
        self.assertEqual(by_slug["agua-coco"]["source"], "manual")
        self.assertFalse(by_slug["feijao-carioca"]["needs_manual"])

    def test_unavailable_and_unit_mismatch_are_pending(self):
        rows = build_admin_rows(
            CATALOG,
            [legacy("Água de Coco", "5.00", "kg")],  # unidade divergente do ingrediente (L)
            [], CATEGORIES)
        by_slug = {row["slug"]: row for row in rows}
        self.assertEqual(by_slug["agua-coco"]["source"], "unavailable")
        self.assertTrue(by_slug["agua-coco"]["needs_manual"])
        self.assertTrue(by_slug["pao-frances"]["needs_manual"])

    def test_group_prefers_food_category_and_keeps_inactive_visible(self):
        rows = build_admin_rows(CATALOG, [], [], CATEGORIES)
        by_slug = {row["slug"]: row for row in rows}
        self.assertEqual(by_slug["feijao-carioca"]["group"], "Leguminosas")
        self.assertEqual(by_slug["pao-frances"]["group"], "pao")
        self.assertEqual(by_slug["agua-coco"]["group"], "")
        self.assertFalse(by_slug["arroz-branco"]["active"])

    def test_price_out_of_bounds_is_treated_as_missing(self):
        rows = build_admin_rows(
            [ingredient("a", "A", "kg")],
            [legacy("A", "0", "kg", "manual")], [], [])
        self.assertIsNone(rows[0]["price_avg"])
        self.assertTrue(rows[0]["needs_manual"])


class AdminEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.fetch = AsyncMock(return_value=[])
        self.conn.execute = AsyncMock(return_value="OK")
        self.conn.executemany = AsyncMock(return_value="OK")
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

    def test_list_requires_token_and_returns_ingredient_rows(self):
        self.assertEqual(self.client.get("/api/admin/grocery-prices").status_code, 401)
        self.conn.fetch.side_effect = [
            CATALOG,
            [legacy("Pão Francês", "12.00", "un", "estimated")],
            [], CATEGORIES, []]
        response = self.client.get("/api/admin/grocery-prices", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], len(CATALOG))
        pao = next(row for row in body["items"] if row["slug"] == "pao-frances")
        self.assertEqual(pao["unit"], "un")
        self.assertEqual(pao["source"], "estimated")
        self.assertEqual(pao["price_avg"], 12.0)

    def test_missing_counts_only_ingredients_without_price(self):
        self.conn.fetch.side_effect = [
            CATALOG,
            [legacy("Pão Francês", "12.00", "un", "estimated")],
            [], CATEGORIES, [{"ingredient_slug": "pao-frances", "total": 3}]]
        body = self.client.get("/api/admin/grocery-prices/missing", headers=self.headers).json()
        self.assertEqual(body["total"], 3)
        self.assertNotIn("pao-frances", [row["slug"] for row in body["items"]])
        self.assertEqual(next(row for row in body["items"] if row["slug"] == "feijao-carioca")["contributions_total"], 0)

    def test_put_by_slug_uses_ingredient_name_and_unit(self):
        self.conn.fetch.return_value = [CATALOG[1]]
        response = self.client.put("/api/admin/grocery-prices/ingredient/pao-frances",
                                   headers=self.headers, json={"price": 8.5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unit"], "un")
        call = self.conn.execute.call_args
        self.assertIn("price_unit", call.args[0])
        self.assertEqual(call.args[1:], ("Pão Francês Tradicional", "pao-frances", Decimal("8.5"), "un"))

    def test_put_rejects_unknown_slug_and_wrong_unit(self):
        self.conn.fetch.return_value = []
        response = self.client.put("/api/admin/grocery-prices/ingredient/desconhecido",
                                   headers=self.headers, json={"price": 8.5})
        self.assertEqual(response.status_code, 422)
        self.conn.fetch.return_value = [CATALOG[1]]
        response = self.client.put("/api/admin/grocery-prices/ingredient/pao-frances",
                                   headers=self.headers, json={"price": 8.5, "price_unit": "kg"})
        self.assertEqual(response.status_code, 422)
        self.conn.execute.assert_not_awaited()

    def test_put_maps_database_failure_to_500(self):
        self.conn.fetch.return_value = [CATALOG[1]]
        self.conn.execute.side_effect = RuntimeError("segredo")
        response = self.client.put("/api/admin/grocery-prices/ingredient/pao-frances",
                                   headers=self.headers, json={"price": 8.5})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("segredo", response.text)

    def test_import_resolves_slug_name_and_reports_bad_rows(self):
        self.conn.fetch.return_value = [item for item in CATALOG if item["active"]]
        content = "slug,price\npao-frances,8.50\nFeijão Carioca,9.90\narroz-branco,4.00\npao-frances,7.00\n".encode()
        body = self.client.post("/api/admin/grocery-prices/import-ingredients",
                                headers=self.headers, content=content).json()
        self.assertEqual(body["importados"], 2)
        self.assertEqual(body["erros"], 2)
        call = self.conn.executemany.call_args
        self.assertIn("price_unit", call.args[0])
        self.assertEqual(call.args[1], [("Pão Francês Tradicional", "pao-frances", Decimal("8.5"), "un"),
                                        ("Feijão Carioca", "feijao-carioca", Decimal("9.90"), "kg")])

    def test_import_json_accepts_names_and_rejects_unit_mismatch(self):
        self.conn.fetch.return_value = CATALOG
        body = self.client.post("/api/admin/grocery-prices/import-ingredients", headers=self.headers,
                                json=[{"food_name": "Feijão Carioca", "price": "9,90"},
                                      {"food_name": "Água de Coco", "price": 5, "price_unit": "kg"}]).json()
        self.assertEqual(body["importados"], 1)
        self.assertEqual(body["erros"], 1)
        rows = self.conn.executemany.call_args.args[1]
        self.assertEqual(rows[0][3], "kg")

    def test_import_requires_auth_and_valid_document(self):
        self.assertEqual(self.client.post("/api/admin/grocery-prices/import-ingredients",
                                          content="x").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/grocery-prices/import-ingredients",
                                          headers=self.headers, content="invalid").status_code, 422)
        self.conn.fetch.return_value = CATALOG
        self.assertEqual(self.client.post("/api/admin/grocery-prices/import-ingredients",
                                          headers=self.headers, json={"lista": 1}).status_code, 422)

    def test_import_failure_rolls_back_and_hides_details(self):
        self.conn.fetch.return_value = CATALOG
        self.conn.executemany.side_effect = RuntimeError("segredo")
        response = self.client.post("/api/admin/grocery-prices/import-ingredients",
                                    headers=self.headers, json=[{"slug": "pao-frances", "price": 8.5}])
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("segredo", response.text)
        self.conn.transaction.return_value.__aexit__.assert_awaited_once()


class CrudIngredientTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.fetch = AsyncMock(return_value=CATALOG)
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=9)
        self.conn.execute = AsyncMock(return_value="OK")
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
        self.headers = {"Authorization": "Bearer test-only-token", "Content-Type": "application/json"}

    def create(self, payload):
        return self.client.post("/api/admin/grocery-ingredients", headers=self.headers, json=payload)

    def test_create_ingredient_generates_slug_and_bumps_revision(self):
        response = self.create({"name": "Iogurte de Coco", "unit": "un", "aliases": ["Coco fresco"],
                                "grams_per_unit": 170, "group": "laticinios"})
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["slug"], "iogurte-de-coco")
        self.assertEqual(item["unit"], "un")
        self.assertEqual(item["revision"], 9)
        call = self.conn.execute.call_args
        self.assertIn("INSERT INTO grocery_ingredients", call.args[0])
        details = json.loads(call.args[4])
        self.assertEqual(details["aliases"], ["Coco fresco"])
        self.assertEqual(details["grams_per_unit"], 170.0)
        self.assertEqual(details["canonical_group_slug"], "laticinios")

    def test_create_rejects_duplicate_name_and_invalid_payload(self):
        response = self.create({"name": "Feijão Carioca", "unit": "kg"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("nome ou sinônimo", response.json()["detail"])
        self.create({"name": "Novo", "unit": "gramas"})
        self.create({"name": "Novo", "unit": "kg", "grams_per_unit": -1})
        self.create({"name": "Novo", "unit": "kg", "surpresa": 1})
        self.conn.execute.assert_not_awaited()

    def test_create_rejects_food_ids_outside_catalog(self):
        self.conn.fetch.side_effect = [CATALOG, [{"remote_id": "food_150"}]]
        response = self.create({"name": "Novo Item", "unit": "kg", "food_ids": ["food_999"]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("fora do catálogo", response.json()["detail"])
        self.conn.execute.assert_not_awaited()

    def test_update_preserves_untouched_details_and_warns_on_unit_change(self):
        self.conn.fetchrow.return_value = {"slug": "feijao-carioca", "name": "Feijão Carioca",
                                           "unit": "kg", "details": {"aliases": ["Feijão Carioca Cozido"],
                                                                     "grams_per_unit": 1000}, "active": True}
        response = self.client.patch("/api/admin/grocery-ingredients/feijao-carioca", headers=self.headers,
                                     json={"grams_per_unit": 500, "unit": "un"})
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["revision"], 9)
        self.assertEqual(len(item["avisos"]), 1)
        self.assertIn("Unidade alterada", item["avisos"][0])
        details = json.loads(self.conn.execute.call_args.args[4])
        self.assertEqual(details["aliases"], ["Feijão Carioca Cozido"])
        self.assertEqual(details["grams_per_unit"], 500.0)

    def test_update_maps_missing_slug_to_404(self):
        self.conn.fetchrow.return_value = None
        response = self.client.patch("/api/admin/grocery-ingredients/desconhecido",
                                     headers=self.headers, json={"active": False})
        self.assertEqual(response.status_code, 404)
        self.conn.execute.assert_not_awaited()

    def test_deactivate_keeps_row_and_revision_bumps(self):
        self.conn.fetchrow.return_value = {"slug": "pao-frances", "name": "Pão Francês Tradicional",
                                           "unit": "un", "details": {}, "active": True}
        response = self.client.patch("/api/admin/grocery-ingredients/pao-frances",
                                     headers=self.headers, json={"active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["item"]["active"])
        call = self.conn.execute.call_args
        self.assertIn("UPDATE grocery_ingredients", call.args[0])
        self.assertEqual(call.args[-1], False)

    def test_delete_is_blocked_when_contributions_exist(self):
        self.conn.fetchrow.return_value = {"slug": "feijao-carioca", "name": "Feijão Carioca",
                                           "unit": "kg", "details": {}, "active": True}
        self.conn.fetchval.return_value = 4
        response = self.client.delete("/api/admin/grocery-ingredients/feijao-carioca", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Desative-o", response.json()["detail"])
        self.conn.execute.assert_not_awaited()

    def test_delete_removes_manual_price_and_ingredient(self):
        self.conn.fetchrow.return_value = {"slug": "arroz-branco", "name": "Arroz Branco",
                                           "unit": "kg", "details": {}, "active": False}
        self.conn.fetchval = AsyncMock(side_effect=[0, 12])
        response = self.client.delete("/api/admin/grocery-ingredients/arroz-branco", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["revision"], 12)
        statements = [call.args[0] for call in self.conn.execute.call_args_list]
        self.assertTrue(any("DELETE FROM grocery_price_cache" in sql for sql in statements))
        self.assertTrue(any("DELETE FROM grocery_ingredients" in sql for sql in statements))

    def test_create_rejects_preparation_terms_in_name(self):
        for bad_name in ["Beterraba Cozida", "Peito de Frango Desfiado", "Cenoura Ralada Crua",
                         "Carne Moída", "Ovo Mexido", "Batata no Vapor", "Tomate Fatiado"]:
            response = self.client.post("/api/admin/grocery-ingredients", headers=self.headers,
                                        json={"name": bad_name, "unit": "kg"})
            self.assertEqual(response.status_code, 422, f"Deveria rejeitar '{bad_name}'")
            self.assertIn("termo de preparo ou corte", response.json()["detail"])

    def test_update_rejects_preparation_terms_in_name(self):
        response = self.client.patch("/api/admin/grocery-ingredients/feijao-carioca", headers=self.headers,
                                     json={"name": "Feijão Carioca Cozido"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("termo de preparo ou corte", response.json()["detail"])

    def test_clean_aliases_strips_self_alias(self):
        self.conn.fetchrow.return_value = None
        self.conn.fetchval = AsyncMock(side_effect=[0, 10])
        response = self.client.post("/api/admin/grocery-ingredients", headers=self.headers,
                                    json={"name": "Beterraba", "unit": "kg",
                                          "aliases": ["Beterraba", "Beterraba Crua", "beterraba"]})
        self.assertEqual(response.status_code, 200)
        details = json.loads(self.conn.execute.call_args_list[0].args[4])
        self.assertEqual(details["aliases"], ["Beterraba Crua"])


class Migration045And046ContractTests(unittest.TestCase):
    def setUp(self):
        self.migrations_dir = Path(__file__).resolve().parents[2] / "migrations"

    def test_migration_files_exist_with_matching_rollbacks(self):
        for version in ["045_grocery_prices_dedupe", "046_canonical_product_renames"]:
            migration_file = self.migrations_dir / f"{version}.sql"
            rollback_file = self.migrations_dir / f"{version}_rollback.sql"
            self.assertTrue(migration_file.exists(), f"Arquivo {migration_file} deve existir.")
            self.assertTrue(rollback_file.exists(), f"Arquivo {rollback_file} deve existir.")

            migration_content = migration_file.read_text(encoding="utf-8")
            rollback_content = rollback_file.read_text(encoding="utf-8")

            self.assertIn("BEGIN;", migration_content)
            self.assertIn("COMMIT;", migration_content)
            self.assertIn("nutrition_catalog_settings", migration_content)
            self.assertIn("revision = revision + 1", migration_content)

            self.assertIn("BEGIN;", rollback_content)
            self.assertIn("COMMIT;", rollback_content)

    def test_migration_046_canonical_names_pass_admin_validation(self):
        from services.ingredient_admin import _validate_clean_name
        canonical_names = [
            "Gérmen de Trigo", "Linhaça Dourada", "Linhaça Marrom",
            "Lombo Suíno Magro", "Macadâmia", "Músculo Bovino Magro",
            "Patinho Bovino", "Pimenta-do-Reino", "Rabanete",
            "Repolho Roxo", "Sardinha em Conserva com Molho de Tomate",
            "Sementes de Abóbora"
        ]
        for name in canonical_names:
            validated = _validate_clean_name(name)
            self.assertEqual(validated, name)


if __name__ == "__main__":
    unittest.main()
