BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_market_prices DROP COLUMN last_verified_at, DROP COLUMN in_stock, DROP COLUMN product_sku;
ALTER TABLE grocery_markets DROP COLUMN cart_url_template, DROP COLUMN cart_type;
COMMIT;
