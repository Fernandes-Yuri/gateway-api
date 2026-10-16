"""Retail nutrition values are totals per package, prices are BRL per package."""
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, AwareDatetime, model_validator


class MarketIn(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    logo_url: HttpUrl
    website_url: HttpUrl
    active: bool = True


class MarketQuote(BaseModel):
    market_id: str
    market_name: str
    market_logo_url: str
    website_url: str
    product_store_url: Optional[str] = None
    price: Decimal
    last_updated: datetime


class GroceryRetailCardIn(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    image_url: HttpUrl
    package_display: str = Field(min_length=1, max_length=100)
    package_net_value: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    package_unit: Literal['g', 'ml', 'un']
    kcal_per_package: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    carbs_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    protein_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    fiber_g: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    fat_g: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    price_avg: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    linked_ingredient_slug: Optional[str] = None
    clinical_tags: list[str] = Field(default_factory=list)
    active: bool = True

    @model_validator(mode='after')
    def whole_units(self):
        if self.package_unit == 'un' and self.package_net_value != self.package_net_value.to_integral_value():
            raise ValueError('Embalagens em unidades exigem conteúdo inteiro.')
        return self


class GroceryRetailCardOut(GroceryRetailCardIn):
    markets: list[MarketQuote] = Field(default_factory=list)


class PriceIn(BaseModel):
    card_id: str
    market_id: str
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    product_store_url: Optional[HttpUrl] = None
    last_updated: AwareDatetime  # Actual research time, never silently refreshed by import.


class GroceryImport(BaseModel):
    markets: list[MarketIn] = Field(default_factory=list, max_length=100)
    cards: list[GroceryRetailCardIn] = Field(default_factory=list, max_length=5000)
    prices: list[PriceIn] = Field(default_factory=list, max_length=20000)


class BasketItemIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=999, strict=True)


class BasketComparisonRequest(BaseModel):
    items: list[BasketItemIn] = Field(max_length=500)


class MarketBasketTotal(BaseModel):
    market_id: str
    market_name: str
    market_logo_url: str
    website_url: str
    total_brl: Decimal
    missing_items_count: int = 0
    is_best_deal: bool = False
    savings_vs_highest_brl: Decimal = Decimal('0')


class BasketComparisonResult(BaseModel):
    total_items: int
    total_planned_kcal: Decimal
    best_market: Optional[MarketBasketTotal] = None
    market_rankings: list[MarketBasketTotal] = Field(default_factory=list)


class GroceryAdminMutation(BaseModel):
    kind: Literal['card', 'market', 'quote', 'remove_quote']
    payload: dict
