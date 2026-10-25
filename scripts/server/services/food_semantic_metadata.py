"""Semantic contract for food planning. Values are curated in the catalog, never inferred."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FoodSemanticMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pillar: Literal["GRAINS_TUBERS", "PROTEIN", "PRODUCE", "PANTRY_FATS"]
    mealRole: Literal["STAPLE_BASE", "PRIMARY_PROTEIN", "COMPLEMENTARY_PROTEIN", "VEGETABLE_SOLID", "VEGETABLE_LEAFY", "FRUIT_DENSE", "FRUIT_HYDRATING", "BREAKFAST_BASE", "PANTRY_SEASONING"]
    proteinSubtype: Literal["POULTRY", "BEEF", "PORK", "FISH", "EGGS", "PLANT_BASED", "DAIRY", "NONE"]
    shelfLife: Literal["PERISHABLE_FAST", "PERISHABLE_DURABLE", "SHELF_STABLE"]
    costTier: Literal["ESSENTIAL", "REGULAR", "PREMIUM"]
    isPantryStaple: bool


class FoodPlanningMetadata(BaseModel):
    semanticMetadata: FoodSemanticMetadata
    maxDailyGrams: int = Field(gt=0)
