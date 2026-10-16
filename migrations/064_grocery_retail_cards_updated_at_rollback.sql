BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_retail_cards DROP COLUMN updated_at;
COMMIT;
