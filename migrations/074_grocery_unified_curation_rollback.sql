BEGIN;
SET LOCAL lock_timeout = '10s';
-- Refuse rollback if unquoted (zero-price) packages remain. Never invent prices.
ALTER TABLE grocery_retail_cards
 DROP CONSTRAINT grocery_retail_cards_price_avg_check,
 ADD CONSTRAINT grocery_retail_cards_price_avg_check CHECK (price_avg > 0),
 DROP COLUMN barcode_ean;
COMMIT;
