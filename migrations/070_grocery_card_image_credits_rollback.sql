BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE grocery_retail_cards
  DROP COLUMN IF EXISTS image_credit,
  DROP COLUMN IF EXISTS image_credit_url;

DELETE FROM schema_migrations WHERE version = '070_grocery_card_image_credits';

COMMIT;
