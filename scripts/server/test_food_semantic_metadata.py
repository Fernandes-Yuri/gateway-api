"""Contract and migration seed checks; no production database access."""
import json
from pathlib import Path
import unittest
from pydantic import ValidationError
from services.food_semantic_metadata import FoodPlanningMetadata


class FoodSemanticMetadataTest(unittest.TestCase):
    def test_all_seeded_identifiers_have_valid_explicit_metadata(self):
        root = Path(__file__).resolve().parents[2]
        sql = (root / "migrations/072_food_semantic_metadata.sql").read_text()
        rows = json.loads(sql.split("$metadata$")[1])
        self.assertEqual(242, len(rows))
        self.assertEqual(len(rows), len({row["remoteId"] for row in rows}))
        for row in rows:
            FoodPlanningMetadata.model_validate(row)

    def test_contract_rejects_missing_semantics_invalid_enum_and_nonpositive_cap(self):
        with self.assertRaises(ValidationError):
            FoodPlanningMetadata.model_validate({"maxDailyGrams": 20})
        valid = dict(pillar="PRODUCE", mealRole="VEGETABLE_LEAFY", proteinSubtype="NONE",
                     shelfLife="PERISHABLE_FAST", costTier="ESSENTIAL", isPantryStaple=False)
        for data in ({"semanticMetadata": valid, "maxDailyGrams": 0},
                     {"semanticMetadata": {**valid, "pillar": "nome de alimento"}, "maxDailyGrams": 50}):
            with self.assertRaises(ValidationError):
                FoodPlanningMetadata.model_validate(data)
