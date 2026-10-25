"""Retail nutrition values are totals per package, prices are BRL per package."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, AwareDatetime, model_validator, field_validator, ConfigDict


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
    price_avg: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    linked_ingredient_slug: Optional[str] = None
    clinical_tags: list[str] = Field(default_factory=list)
    barcode_ean: Optional[str] = None

    @field_validator('barcode_ean', mode='before')
    @classmethod
    def valid_ean(cls, value):
        if value is None or value == '':
            return None
        if not isinstance(value, str) or len(value) != 13 or not value.isascii() or not value.isdigit():
            raise ValueError('Informe um EAN-13 válido, com 13 dígitos.')
        check = (10 - sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(value[:12])) % 10) % 10
        if check != int(value[-1]):
            raise ValueError('Dígito verificador do EAN-13 inválido.')
        return value

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


class RetailQuoteInput(BaseModel):
    """A null/blank price explicitly removes this market's quote; omission preserves it."""
    model_config = ConfigDict(extra='forbid')
    market_id: str = Field(min_length=1, max_length=100)
    price: Optional[Decimal] = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    product_store_url: Optional[HttpUrl] = None
    product_sku: Optional[str] = Field(default=None, max_length=200)
    in_stock: bool = True
    last_updated: Optional[AwareDatetime] = None
    last_verified_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator('price', mode='before')
    @classmethod
    def blank_price(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode='after')
    def valid_observation(self):
        if self.price is not None:
            if self.last_updated is None:
                raise ValueError('Informe a data da checagem para cada preço.')
            if self.last_updated > datetime.now(timezone.utc):
                raise ValueError('A data da pesquisa não pode estar no futuro.')
        return self


class GroceryFoodCardIn(GroceryRetailCardIn):
    # Supplied explicitly: manual override. Omitted/null: consolidate active quotes.
    price_avg: Optional[Decimal] = Field(default=None, gt=0, max_digits=10, decimal_places=2)


class GroceryFoodIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    card: GroceryFoodCardIn
    quotes: Optional[list[RetailQuoteInput]] = Field(default=None, max_length=10)
    reference: Optional[dict] = None  # Compatibility with editorial imports and the previous portal.

    @model_validator(mode='after')
    def unique_markets(self):
        if self.quotes is not None:
            if self.reference is not None:
                raise ValueError('Envie cotações em lote ou uma referência, não ambos.')
            if len({quote.market_id for quote in self.quotes}) != len(self.quotes):
                raise ValueError('Informe cada supermercado uma única vez.')
        return self


class GroceryImport(BaseModel):
    markets: list[MarketIn] = Field(default_factory=list, max_length=100)
    cards: list[GroceryRetailCardIn] = Field(default_factory=list, max_length=5000)
    prices: list[PriceIn] = Field(default_factory=list, max_length=20000)


class GroceryAdminMutation(BaseModel):
    kind: Literal['food', 'card', 'card_with_ingredient', 'market', 'quote', 'remove_quote']
    payload: dict


class BasketItemIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=999, strict=True)


class BasketComparisonIn(BaseModel):
    items: list[BasketItemIn] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_cards(self):
        if len({item.card_id for item in self.items}) != len(self.items):
            raise ValueError('Informe cada produto uma única vez.')
        return self
