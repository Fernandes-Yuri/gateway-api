"""Catálogo de ingredientes compráveis, separado dos preparos clínicos."""
import json
from pathlib import Path
from decimal import Decimal
from unicodedata import normalize, combining

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "grocery_ingredients.json").read_text(encoding="utf-8"))
INGREDIENTS = {x["slug"]: x for x in MANIFEST["ingredients"]}

def key(value):
    return " ".join("".join(c for c in normalize("NFD", value) if not combining(c)).casefold().split())

FOOD_MAPPING = {key(x["food_name"]): x for x in MANIFEST["foods"]}
ALIASES = {key(name): item for item in INGREDIENTS.values() for name in [item["name"], item["slug"], *item["aliases"]]}

async def prices_for_ingredients(pool, averages_sql):
    async with pool.acquire() as conn:
        legacy = await conn.fetch("SELECT food_name,price_avg,price_min,price_max,source,price_unit,updated_at FROM grocery_price_cache WHERE price_avg IS NOT NULL")
        community = await conn.fetch(averages_sql)
    histories = {}
    for row in legacy:
        ingredient = ALIASES.get(key(row["food_name"]))
        if ingredient and row["price_unit"] == ingredient["unit"]:
            histories.setdefault(ingredient["slug"], []).append(row)
    references = {(key(row["food_name"]), row["unit"]): row for row in community}
    result = []
    for item in INGREDIENTS.values():
        sample = references.get((key(item["name"]), item["unit"]))
        candidates = histories.get(item["slug"], [])
        # Preços manuais com unidade explícita continuam prioritários.
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
