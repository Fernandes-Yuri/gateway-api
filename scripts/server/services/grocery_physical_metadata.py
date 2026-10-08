"""Contrato opcional de propriedades físicas do catálogo, sem tabela fixa de alimentos."""
from math import isclose
from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngredientPhysicalMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    grams_per_unit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    density_g_per_ml: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    canonical_group_slug: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def group_not_blank(self):
        if self.canonical_group_slug is not None and not self.canonical_group_slug.strip():
            raise ValueError("Agrupador canônico vazio")
        return self


class FoodComponentMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    shoppingComponents: list[str]
    component_proportions: dict[str, float] | None = None

    @model_validator(mode="after")
    def valid_proportions(self):
        values = self.component_proportions
        if values is not None:
            from math import isfinite
            if (set(values) != set(self.shoppingComponents) or
                    any(not isfinite(v) or v <= 0 for v in values.values()) or
                    not isclose(sum(values.values()), 1.0, rel_tol=0, abs_tol=0.000001)):
                raise ValueError("Proporções devem cobrir os componentes e somar 1")
        return self
