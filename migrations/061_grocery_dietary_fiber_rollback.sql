BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE grocery_retail_cards DROP COLUMN fiber_g;
COMMIT;
