"""Catálogo de alimentos e ingredientes servido a partir do RDS."""
import json
from decimal import Decimal
from services.grocery_physical_metadata import IngredientPhysicalMetadata, FoodComponentMetadata
from unicodedata import normalize, combining


def key(value):
    return " ".join("".join(c for c in normalize("NFD", value) if not combining(c)).casefold().split())


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def load_ingredients(conn, *, active_only=True):
    rows = await conn.fetch("SELECT slug,name,unit,details,active FROM grocery_ingredients " +
                            ("WHERE active " if active_only else "") + "ORDER BY name,slug")
    result = []
    for row in rows:
        details = as_json(row["details"]) or {}
        physical = IngredientPhysicalMetadata.model_validate(details).model_dump()
        result.append({"step": 1, "minimum": 50, "aliases": [], "food_ids": [],
                       **details, **physical, "slug": row["slug"], "name": row["name"],
                       "unit": row["unit"], "active": bool(row["active"])})
    return result


def aliases_for(ingredients):
    return {key(name): item for item in ingredients for name in [item["name"], item["slug"], *item["aliases"]]}


async def resolve_ingredient(conn, name):
    item = aliases_for(await load_ingredients(conn)).get(key(name))
    if item is None:
        raise ValueError("Selecione um ingrediente ativo do catálogo para contribuir.")
    return item


def _latest(rows):
    """Linha mais recente entre candidatos; updated_at nulo vai para o fim."""
    if not rows:
        return None
    return max(rows, key=lambda r: (r["updated_at"] is not None, r["updated_at"]))


def _price_of(row):
    """Preço médio da linha quando dentro dos limites aceitos; None fora."""
    if not row or row["price_avg"] is None:
        return None
    price = float(row["price_avg"])
    if not Decimal("0") < Decimal(str(price)) <= Decimal("99999999.99"):
        return None
    return price


def build_admin_rows(ingredients, legacy, community, categories=(), contributions=None):
    """Monta a visão administrativa por ingrediente (pura, sem banco).

    Prioridade idêntica à do app (enrich_prices): manual > comunidade >
    estimado legado; sem preço, fica indisponível (pendente de definição).
    """
    contributions = contributions or {}
    slug_map = {item["slug"]: item for item in ingredients}
    aliases = aliases_for(ingredients)
    histories = {}
    for row in legacy:
        slug = row.get("ingredient_slug") if hasattr(row, "get") else (row["ingredient_slug"] if "ingredient_slug" in row else None)
        ingredient = (slug_map.get(slug) if slug else None) or aliases.get(key(row["food_name"]))
        if ingredient and row["price_unit"] == ingredient["unit"]:
            histories.setdefault(ingredient["slug"], []).append(row)
    references = {(key(row["food_name"]), row["unit"]): row for row in community}
    groups = {row["remote_id"]: row["category"] for row in categories}
    result = []
    for item in ingredients:
        sample = references.get((key(item["name"]), item["unit"]))
        candidates = histories.get(item["slug"], [])
        manual = [r for r in candidates if r["source"] == "manual"]
        latest = _latest(manual or candidates)
        row = latest if manual else sample or latest
        source = "manual" if manual else "community" if sample else "estimated" if latest else "unavailable"
        result.append({
            "slug": item["slug"], "name": item["name"], "unit": item["unit"],
            "group": _group_of(item, groups), "active": item["active"],
            "price_avg": _price_of(row),
            "price_min": float(row["price_min"]) if row and row["price_min"] is not None else None,
            "price_max": float(row["price_max"]) if row and row["price_max"] is not None else None,
            "source": source,
            "updated_at": row["updated_at"].isoformat() if row and row.get("updated_at") else "",
            "contributors": int(sample["contributors"]) if sample and source == "community" else 0,
            "contributions_total": int(contributions.get(item["slug"], 0)),
            "needs_manual": _price_of(row) is None,
        })
    return result


def _group_of(item, groups):
    """Categoria do alimento ligado ao ingrediente; agrupador canônico é plano B."""
    for food_id in item.get("food_ids") or []:
        if groups.get(food_id):
            return groups[food_id]
    return item.get("canonical_group_slug") or ""


async def admin_price_rows(conn, averages_sql):
    """Linhas administrativas com o mesmo snapshot repetível do enriquecimento."""
    ingredients = await load_ingredients(conn, active_only=False)
    legacy = await conn.fetch("SELECT food_name,ingredient_slug,price_avg,price_min,price_max,source,price_unit,updated_at FROM grocery_price_cache")
    community = await conn.fetch(averages_sql)
    categories = await conn.fetch("SELECT remote_id,details->>'category' AS category FROM food_catalog WHERE active")
    counts = await conn.fetch("SELECT ingredient_slug, COUNT(*) AS total FROM grocery_price_contributions GROUP BY ingredient_slug")
    contributions = {row["ingredient_slug"]: row["total"] for row in counts}
    return build_admin_rows(ingredients, legacy, community, categories, contributions)


async def enrich_prices(conn, ingredients, averages_sql):
    legacy = await conn.fetch("SELECT food_name,ingredient_slug,price_avg,price_min,price_max,source,price_unit,updated_at FROM grocery_price_cache WHERE price_avg IS NOT NULL")
    community = await conn.fetch(averages_sql)
    slug_map = {item["slug"]: item for item in ingredients}
    aliases = aliases_for(ingredients)
    histories = {}
    for row in legacy:
        slug = row.get("ingredient_slug") if hasattr(row, "get") else (row["ingredient_slug"] if "ingredient_slug" in row else None)
        ingredient = (slug_map.get(slug) if slug else None) or aliases.get(key(row["food_name"]))
        if ingredient and row["price_unit"] == ingredient["unit"]:
            histories.setdefault(ingredient["slug"], []).append(row)
    references = {(key(row["food_name"]), row["unit"]): row for row in community}
    result = []
    for item in ingredients:
        sample = references.get((key(item["name"]), item["unit"]))
        candidates = histories.get(item["slug"], [])
        manual = [r for r in candidates if r["source"] == "manual"]
        latest = _latest(manual or candidates)
        row = latest if manual else sample or latest
        price = _price_of(row)
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
            for food in foods:
                food["component_proportions"] = FoodComponentMetadata.model_validate(food).component_proportions
            if not settings or not foods:
                raise ValueError("Catálogo nutricional não configurado.")
            return {"version": settings["revision"], "foods": foods, "ingredients": ingredients,
                    "policy": as_json(settings["policy"]),
                    "notice": "Estimativas para planejamento; valores variam por região, loja e data."}
