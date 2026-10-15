BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE grocery_markets (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, logo_url TEXT NOT NULL,
 website_url TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE grocery_retail_cards (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, image_url TEXT NOT NULL,
 package_display TEXT NOT NULL, package_net_value NUMERIC(10,2) NOT NULL CHECK (package_net_value > 0),
 package_unit TEXT NOT NULL CHECK (package_unit IN ('g','ml','un')),
 CHECK (package_unit <> 'un' OR package_net_value = trunc(package_net_value)),
 kcal_per_package NUMERIC(12,2) NOT NULL CHECK (kcal_per_package >= 0),
 carbs_g NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (carbs_g >= 0),
 protein_g NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (protein_g >= 0),
 fat_g NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (fat_g >= 0),
 price_avg NUMERIC(10,2) NOT NULL CHECK (price_avg > 0),
 linked_ingredient_slug TEXT REFERENCES grocery_ingredients(slug),
 clinical_tags TEXT[] NOT NULL DEFAULT '{}', active BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE grocery_market_prices (
 card_id TEXT NOT NULL REFERENCES grocery_retail_cards(id) ON DELETE CASCADE,
 market_id TEXT NOT NULL REFERENCES grocery_markets(id) ON DELETE CASCADE,
 price NUMERIC(10,2) NOT NULL CHECK (price > 0), product_store_url TEXT,
 last_updated TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (card_id, market_id)
);
COMMIT;
