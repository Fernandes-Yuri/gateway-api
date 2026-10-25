"""Retail nutrition values are totals per package, prices are BRL per package."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, AwareDatetime, model_validator


class MarketIn(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    logo_url: HttpUrl | Literal[""] = ""
    website_url: HttpUrl | Literal[""] = ""
    active: bool = True
    cart_type: Literal['VTEX', 'SHOPIFY', 'CUSTOM_PARAM', 'ASSISTED'] = 'VTEX'
    cart_url_template: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def valid_cart_template(self):
        if self.cart_url_template:
            from urllib.parse import urlsplit
            parsed = urlsplit(self.cart_url_template)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Informe uma URL HTTP ou HTTPS de carrinho válida.')
        return self


class MarketQuote(BaseModel):
    market_id: str
    market_name: str
    market_logo_url: str
    website_url: str
    product_store_url: Optional[str] = None
    product_sku: Optional[str] = None
    in_stock: bool = True
    last_verified_at: Optional[datetime] = None
    price: Decimal
    last_updated: datetime


class GroceryRetailCardIn(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    image_url: HttpUrl | Literal[""] = ""
    package_display: str = Field(min_length=1, max_length=100)
    package_net_value: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    package_unit: Literal['g', 'ml', 'un']
    kcal_per_package: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    carbs_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    protein_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    fiber_g: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    fat_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    nutrition_facts: list[dict] = Field(default_factory=list, max_length=50)
    price_avg: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    linked_ingredient_slug: Optional[str] = None
    clinical_tags: list[str] = Field(default_factory=list)
    image_credit: Optional[str] = Field(default=None, max_length=500)
    image_credit_url: Optional[str] = Field(default=None, max_length=1000)
    active: bool = True

    @model_validator(mode='before')
    @classmethod
    def nutrition_facts_compat(cls, value):
        if isinstance(value, dict) and 'nutrition_facts' not in value:
            facts = [
                {'key': 'calories', 'label': 'Calorias', 'unit': 'kcal', 'value': value.get('kcal_per_package', 0)},
                {'key': 'carbohydrates', 'label': 'Carboidratos', 'unit': 'g', 'value': value.get('carbs_g', 0)},
                {'key': 'protein', 'label': 'Proteínas', 'unit': 'g', 'value': value.get('protein_g', 0)},
                {'key': 'fat', 'label': 'Gorduras', 'unit': 'g', 'value': value.get('fat_g', 0)},
            ]
            if value.get('fiber_g') is not None:
                facts.append({'key': 'fiber', 'label': 'Fibras alimentares', 'unit': 'g', 'value': value['fiber_g']})
            return {**value, 'nutrition_facts': facts}
        return value

    @model_validator(mode='after')
    def whole_units(self):
        if self.package_unit == 'un' and self.package_net_value != self.package_net_value.to_integral_value():
            raise ValueError('Embalagens em unidades exigem conteúdo inteiro.')
        return self


class GroceryRetailCardOut(GroceryRetailCardIn):
    updated_at: AwareDatetime
    markets: list[MarketQuote] = Field(default_factory=list)


class PriceIn(BaseModel):
    card_id: str
    market_id: str
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    product_store_url: Optional[HttpUrl] = None
    product_sku: Optional[str] = Field(default=None, max_length=200)
    in_stock: bool = True
    last_verified_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: AwareDatetime  # Actual research time, never silently refreshed by import.


class GroceryImport(BaseModel):
    markets: list[MarketIn] = Field(default_factory=list, max_length=100)
    cards: list[GroceryRetailCardIn] = Field(default_factory=list, max_length=5000)
    prices: list[PriceIn] = Field(default_factory=list, max_length=20000)


class GroceryAdminMutation(BaseModel):
    kind: Literal['food', 'card', 'card_with_ingredient', 'market', 'quote', 'remove_quote']
    payload: dict
