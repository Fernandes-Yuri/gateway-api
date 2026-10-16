"""Editorial work is private and independent from live retail prices."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field, AwareDatetime, HttpUrl, ConfigDict, model_validator, field_validator


class EditionFields(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    starts_on: date
    ends_on: date
    region: str = Field(min_length=1, max_length=200)
    notes: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def dates(self):
        if self.ends_on < self.starts_on:
            raise ValueError('O fim da edição deve ser igual ou posterior ao início.')
        if (self.ends_on - self.starts_on).days > 31:
            raise ValueError('A edição deve durar no máximo 32 dias.')
        return self


class EditionCreate(EditionFields):
    source_edition_id: str | None = Field(default=None, min_length=1, max_length=100)


class EditorialQuote(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    card_id: str = Field(min_length=1, max_length=100)
    market_id: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    product_store_url: HttpUrl | None = None
    researched_at: AwareDatetime
    confirmed: bool = False
    conditions: str = Field(default='', max_length=2000)
    minimum_quantity: int = Field(default=1, ge=1, le=999, strict=True)
    membership_required: bool = False

    @model_validator(mode='after')
    def research(self):
        if self.researched_at > datetime.now(timezone.utc):
            raise ValueError('A pesquisa não pode estar no futuro.')
        if self.confirmed and not self.product_store_url:
            raise ValueError('Informe o link pesquisado antes de confirmar o preço.')
        return self


class QuoteKey(BaseModel):
    model_config = ConfigDict(extra='forbid')
    card_id: str = Field(min_length=1, max_length=100)
    market_id: str = Field(min_length=1, max_length=100)


class PackItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    card_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=999, strict=True)


class PackDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str | None = Field(default=None, min_length=1, max_length=100)
    market_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    cover_url: HttpUrl | None = None
    planning_days: Literal[7, 14, 30] = 7
    pack_type: Literal['complete', 'category'] = 'complete'
    category: str | None = Field(default=None, max_length=100)
    items: list[PackItem] = Field(default_factory=list, max_length=500)

    @field_validator('planning_days', mode='before')
    @classmethod
    def valid_planning_days(cls, value):
        if type(value) is not int:
            raise ValueError('O período deve ser 7, 14 ou 30 dias, em número inteiro.')
        return value

    @model_validator(mode='after')
    def composition(self):
        if self.pack_type == 'category' and not self.category:
            raise ValueError('Escolha a categoria do pack.')
        if len({i.card_id for i in self.items}) != len(self.items):
            raise ValueError('Cada produto deve aparecer uma vez no pack.')
        if self.cover_url and self.cover_url.scheme != 'https':
            raise ValueError('Use HTTPS na imagem de capa.')
        return self


class PackKey(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)


class EditorialMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    operation: Literal['metadata', 'quote', 'remove_quote', 'pack', 'remove_pack']
    payload: dict


class PublishEdition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_publication_id: str | None
    review_digest: str = Field(min_length=64, max_length=64)
    pack_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def unique_packs(self):
        if len(set(self.pack_ids)) != len(self.pack_ids):
            raise ValueError('Não repita packs na publicação.')
        return self


class ChangePublication(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['withdraw', 'restore']
    expected_publication_id: str | None
    publication_id: str = Field(min_length=1, max_length=100)
