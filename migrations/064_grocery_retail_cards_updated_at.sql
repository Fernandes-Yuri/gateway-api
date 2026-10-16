BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_retail_cards
  ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
UPDATE grocery_retail_cards
SET updated_at = created_at;
COMMIT;
