BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_retail_cards
  ADD COLUMN nutrition_facts JSONB NOT NULL DEFAULT '[]'::jsonb;
UPDATE grocery_retail_cards
SET nutrition_facts = jsonb_build_array(
  jsonb_build_object('key','calories','label','Calorias','unit','kcal','value',kcal_per_package),
  jsonb_build_object('key','carbohydrates','label','Carboidratos','unit','g','value',carbs_g),
  jsonb_build_object('key','protein','label','Proteínas','unit','g','value',protein_g),
  jsonb_build_object('key','fat','label','Gorduras','unit','g','value',fat_g)
) || CASE WHEN fiber_g IS NULL THEN '[]'::jsonb ELSE
  jsonb_build_array(jsonb_build_object('key','fiber','label','Fibras alimentares','unit','g','value',fiber_g)) END;
COMMIT;
