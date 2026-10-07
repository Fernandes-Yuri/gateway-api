"""Contribuições opcionais de compras, separadas por unidade e pessoa."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo
import json
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator
from services.ingredient_catalog import ALIASES, INGREDIENTS, key

STATES = set("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())

def normalize_name(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", value) if not unicodedata.combining(c)).casefold().split())

_FOOD_DATA = json.loads((Path(__file__).resolve().parents[1] / "grocery_search_terms.json").read_text(encoding="utf-8"))
KNOWN_FOODS = {normalize_name(x["food_name"]): x["food_name"] for x in _FOOD_DATA}
# Pacotes de pesos diferentes não podem compartilhar uma referência por unidade.
UNIT_FOODS = {x["food_name"] for x in _FOOD_DATA if any(term in x["search_term"] for term in ("duzia", "unidade", "maco"))} | {
    item["name"] for item in INGREDIENTS.values() if item["unit"] == "un"
}

class GroceryPriceContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    food_name: str = Field(min_length=1, max_length=500)
    amount_paid: Decimal = Field(gt=0, le=100000, decimal_places=2, allow_inf_nan=False)
    quantity: Decimal = Field(gt=0, le=100000, decimal_places=3, allow_inf_nan=False)
    unit: Literal["g", "kg", "ml", "L", "un"]
    state: str | None = None
    purchase_date: date

    @field_validator("food_name")
    @classmethod
    def known_food(cls, value):
        name = KNOWN_FOODS.get(normalize_name(value))
        if name is None and normalize_name(value) in ALIASES:
            name = ALIASES[normalize_name(value)]["aliases"][0]
        if name is None:
            raise ValueError("Selecione um alimento do catálogo para contribuir.")
        return name

    @field_validator("state")
    @classmethod
    def valid_state(cls, value):
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if value not in STATES:
            raise ValueError("Informe uma UF brasileira válida.")
        return value

    @field_validator("purchase_date")
    @classmethod
    def recent_purchase(cls, value):
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        if not today - timedelta(days=90) <= value <= today:
            raise ValueError("Informe uma compra dos últimos 90 dias, sem data futura.")
        return value

    @field_validator("quantity")
    @classmethod
    def reject_tiny(cls, value):
        if value < Decimal("0.001"):
            raise ValueError("Quantidade mínima: 0,001.")
        return value

    @property
    def ingredient_slug(self):
        item = ALIASES.get(key(self.food_name))
        return item["slug"] if item else None

    def normalized(self):
        if not self.ingredient_slug:
            raise ValueError("Informe o ingrediente comprado, não o prato preparado.")
        if self.unit == "un" and self.food_name not in UNIT_FOODS:
            raise ValueError("Para produtos embalados, informe o peso ou volume total, não a quantidade de pacotes.")
        if self.unit == "un" and self.quantity != self.quantity.to_integral_value():
            raise ValueError("A quantidade em unidades deve ser inteira.")
        unit = {"g": "kg", "ml": "L"}.get(self.unit, self.unit)
        quantity = self.quantity / 1000 if self.unit in ("g", "ml") else self.quantity
        price = self.amount_paid / quantity
        if not Decimal("0.01") <= price <= Decimal("100000"):
            raise ValueError("Confira o preço, o peso e a unidade informados.")
        return unit, price

# Uma contribuição recente por pessoa/alimento/unidade. Peso igual entre pessoas.
# A mediana serve apenas para excluir valores extremos; a referência é a média restante.
AVERAGES_SQL = """
WITH latest AS (
 SELECT DISTINCT ON (c.user_id,c.ingredient_slug,c.normalized_unit)
  c.user_id,c.ingredient_slug,i.name AS food_name,c.normalized_unit,c.normalized_price,c.state
 FROM grocery_price_contributions c JOIN grocery_ingredients i ON i.slug=c.ingredient_slug
 WHERE c.purchase_date >= CURRENT_DATE - 90
 ORDER BY c.user_id,c.ingredient_slug,c.normalized_unit,c.purchase_date DESC,c.updated_at DESC
), medians AS (
 SELECT ingredient_slug,normalized_unit,
 percentile_cont(0.5) WITHIN GROUP(ORDER BY normalized_price) AS median_price
 FROM latest GROUP BY ingredient_slug,normalized_unit
), accepted AS (
 SELECT l.* FROM latest l JOIN medians m USING(ingredient_slug,normalized_unit)
 WHERE l.normalized_price BETWEEN m.median_price/3 AND m.median_price*3
)
SELECT MAX(food_name) AS food_name,ingredient_slug,normalized_unit AS unit,
 AVG(normalized_price) AS price_avg,MIN(normalized_price) AS price_min,
 MAX(normalized_price) AS price_max,COUNT(*) AS contributors,COUNT(DISTINCT state) AS states_count
FROM accepted GROUP BY ingredient_slug,normalized_unit HAVING COUNT(*) >= 5
ORDER BY ingredient_slug,normalized_unit
"""

UPSERT_SQL = """
INSERT INTO grocery_price_contributions
 (user_id, food_name, amount_paid, quantity, original_unit, normalized_unit,
  normalized_price, state, purchase_date, ingredient_slug)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
ON CONFLICT (user_id, food_name, normalized_unit, purchase_date) DO UPDATE SET
 amount_paid=EXCLUDED.amount_paid, quantity=EXCLUDED.quantity,
 original_unit=EXCLUDED.original_unit, normalized_price=EXCLUDED.normalized_price,
 state=EXCLUDED.state, ingredient_slug=EXCLUDED.ingredient_slug, updated_at=now()
"""
