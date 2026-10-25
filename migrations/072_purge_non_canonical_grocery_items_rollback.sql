-- Rollback da migração 072: Restaura somente registros inativados no expurgo 072.
BEGIN;

SET LOCAL lock_timeout = '10s';

UPDATE grocery_ingredients
SET active = TRUE
WHERE slug IN (SELECT id FROM grocery_curation_072 WHERE entity = 'ingredient');

UPDATE food_catalog
SET active = TRUE
WHERE remote_id IN (SELECT id FROM grocery_curation_072 WHERE entity = 'food');

UPDATE grocery_retail_cards
SET active = TRUE
WHERE id IN (SELECT id FROM grocery_curation_072 WHERE entity = 'card');

UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

DELETE FROM schema_migrations WHERE version = '072_purge_non_canonical_grocery_items';

DROP TABLE IF EXISTS grocery_curation_072;

COMMIT;
