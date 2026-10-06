BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS grocery_price_contributions (
 id BIGSERIAL PRIMARY KEY,
 user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 food_name TEXT NOT NULL REFERENCES grocery_price_cache(food_name) ON DELETE CASCADE,
 amount_paid NUMERIC(12,2) NOT NULL CHECK (amount_paid > 0),
 quantity NUMERIC(12,3) NOT NULL CHECK (quantity > 0),
 original_unit TEXT NOT NULL CHECK (original_unit IN ('g','kg','ml','L','un')),
 normalized_unit TEXT NOT NULL CHECK (normalized_unit IN ('kg','L','un')),
 normalized_price NUMERIC(16,6) NOT NULL CHECK (normalized_price BETWEEN 0.01 AND 100000),
 state VARCHAR(2), purchase_date DATE NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE (user_id,food_name,normalized_unit,purchase_date)
);
CREATE INDEX IF NOT EXISTS grocery_price_contributions_recent_idx
 ON grocery_price_contributions (purchase_date, food_name, normalized_unit);
COMMIT;
