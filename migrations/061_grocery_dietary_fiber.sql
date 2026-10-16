BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE grocery_retail_cards ADD COLUMN fiber_g NUMERIC(12,2) CHECK (fiber_g >= 0);
COMMIT;
