"""Catálogo de alimentos e ingredientes servido a partir do RDS."""
import json
from decimal import Decimal
from unicodedata import normalize, combining


def key(value):
    return " ".join("".join(c for c in normalize("NFD", value) if not combining(c)).casefold().split())


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def load_ingredients(conn, *, active_only=True):
    rows = await conn.fetch("SELECT slug,name,unit,details FROM grocery_ingredients " +
                            ("WHERE active " if active_only else "") + "ORDER BY name,slug")
    return [{"step": 1, "minimum": 50, "aliases": [], "food_ids": [],
             **(as_json(r["details"]) or {}), "slug": r["slug"], "name": r["name"], "unit": r["unit"]} for r in rows]


def aliases_for(ingredients):
    return {key(name): item for item in ingredients for name in [item["name"], item["slug"], *item["aliases"]]}


async def resolve_ingredient(conn, name):
    item = aliases_for(await load_ingredients(conn)).get(key(name))
    if item is None:
        raise ValueError("Selecione um ingrediente ativo do catálogo para contribuir.")
    return item


async def enrich_prices(conn, ingredients, averages_sql):
    legacy = await conn.fetch("SELECT food_name,price_avg,price_min,price_max,source,price_unit,updated_at FROM grocery_price_cache WHERE price_avg IS NOT NULL")
    community = await conn.fetch(averages_sql)
    aliases = aliases_for(ingredients)
    histories = {}
    for row in legacy:
        ingredient = aliases.get(key(row["food_name"]))
        if ingredient and row["price_unit"] == ingredient["unit"]:
            histories.setdefault(ingredient["slug"], []).append(row)
    references = {(key(row["food_name"]), row["unit"]): row for row in community}
    result = []
    for item in ingredients:
        sample = references.get((key(item["name"]), item["unit"]))
        candidates = histories.get(item["slug"], [])
        manual = [r for r in candidates if r["source"] == "manual"]
        latest = max(manual or candidates, key=lambda r: r["updated_at"]) if candidates else None
        row = latest if manual else sample or latest
        price = float(row["price_avg"]) if row else None
        if price is not None and not Decimal("0") < Decimal(str(price)) <= Decimal("99999999.99"):
            price = None
        result.append({**item, "price_avg": price,
            "source": "manual" if manual else "community" if sample else "estimated" if latest else "unavailable",
            "contributors": int(sample["contributors"]) if sample and not manual else 0})
    return result


async def prices_for_ingredients(pool, averages_sql):
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            return await enrich_prices(conn, await load_ingredients(conn), averages_sql)


async def nutrition_snapshot(pool, averages_sql):
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            settings = await conn.fetchrow("SELECT revision,policy FROM nutrition_catalog_settings WHERE id=1")
            rows = await conn.fetch("""SELECT details || jsonb_build_object(
                'remoteId',remote_id,'name',name,'kcal',kcal,'carbsG',carbs,'proteinG',proteins,
                'fatG',fats,'isDiabetesSafe',is_diabetes_safe,'isHypertensionSafe',is_hypertension_safe,
                'isThyroidSafe',is_thyroid_safe) AS food
                FROM food_catalog WHERE active AND details IS NOT NULL ORDER BY remote_id""")
            ingredients = await enrich_prices(conn, await load_ingredients(conn), averages_sql)
            foods = [as_json(r["food"]) for r in rows]
            if not settings or not foods:
                raise ValueError("Catálogo nutricional não configurado.")
            return {"version": settings["revision"], "foods": foods, "ingredients": ingredients,
                    "policy": as_json(settings["policy"]),
                    "notice": "Estimativas para planejamento; valores variam por região, loja e data."}
