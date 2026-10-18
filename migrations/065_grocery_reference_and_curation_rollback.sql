-- Reativa somente registros inativados nesta curadoria. Pesquisas permanecem como histórico.
BEGIN;
SET LOCAL lock_timeout = '10s';
UPDATE grocery_ingredients SET active=TRUE WHERE slug IN(SELECT id FROM grocery_curation_065 WHERE entity='ingredient');
UPDATE food_catalog SET active=TRUE WHERE remote_id IN(SELECT id FROM grocery_curation_065 WHERE entity='food');
UPDATE grocery_retail_cards SET active=TRUE WHERE id IN(SELECT id FROM grocery_curation_065 WHERE entity='card');
UPDATE nutrition_catalog_settings SET revision=revision+1 WHERE id=1;
DROP TABLE grocery_curation_065;
COMMIT;
