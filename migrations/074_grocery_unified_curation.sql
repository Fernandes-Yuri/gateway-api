BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_retail_cards
 ADD COLUMN barcode_ean TEXT CHECK (barcode_ean IS NULL OR barcode_ean ~ '^[0-9]{13}$'),
 DROP CONSTRAINT grocery_retail_cards_price_avg_check,
 ADD CONSTRAINT grocery_retail_cards_price_avg_check CHECK (price_avg >= 0);
-- Zero identifies an unquoted package; no invented fallback price is required.
COMMIT;
