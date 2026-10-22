BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE grocery_retail_cards
  ADD COLUMN IF NOT EXISTS image_credit TEXT,
  ADD COLUMN IF NOT EXISTS image_credit_url TEXT;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
DELETE FROM schema_migrations WHERE version = '070_grocery_card_image_credits';
INSERT INTO schema_migrations (version) VALUES ('070_grocery_card_image_credits');

COMMIT;
