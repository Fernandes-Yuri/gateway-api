BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE grocery_editions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL CHECK(length(trim(title))>0),
 starts_on DATE NOT NULL, ends_on DATE NOT NULL,
 region TEXT NOT NULL CHECK(length(trim(region))>0), notes TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 source_edition_id TEXT REFERENCES grocery_editions(id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(ends_on >= starts_on AND ends_on-starts_on <= 31)
);
CREATE TABLE grocery_edition_quotes (
 edition_id TEXT NOT NULL REFERENCES grocery_editions(id) ON DELETE CASCADE,
 card_id TEXT NOT NULL REFERENCES grocery_retail_cards(id),
 market_id TEXT NOT NULL REFERENCES grocery_markets(id),
 price NUMERIC(10,2) NOT NULL CHECK(price>0), product_store_url TEXT,
 researched_at TIMESTAMPTZ NOT NULL, confirmed BOOLEAN NOT NULL DEFAULT FALSE,
 conditions TEXT NOT NULL DEFAULT '', minimum_quantity INTEGER NOT NULL DEFAULT 1 CHECK(minimum_quantity BETWEEN 1 AND 999),
 membership_required BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(edition_id,card_id,market_id)
);
CREATE TABLE grocery_editorial_packs (
 id TEXT PRIMARY KEY, edition_id TEXT NOT NULL REFERENCES grocery_editions(id) ON DELETE CASCADE,
 market_id TEXT NOT NULL REFERENCES grocery_markets(id),
 name TEXT NOT NULL CHECK(length(trim(name))>0), description TEXT NOT NULL DEFAULT '',
 cover_url TEXT, pack_type TEXT NOT NULL CHECK(pack_type IN ('complete','category')),
 category TEXT, CHECK(pack_type <> 'category' OR (category IS NOT NULL AND length(trim(category))>0))
);
CREATE INDEX grocery_editorial_packs_edition_idx ON grocery_editorial_packs(edition_id);
CREATE TABLE grocery_editorial_pack_items (
 pack_id TEXT NOT NULL REFERENCES grocery_editorial_packs(id) ON DELETE CASCADE,
 card_id TEXT NOT NULL REFERENCES grocery_retail_cards(id), quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 999),
 position INTEGER NOT NULL CHECK(position>=0), PRIMARY KEY(pack_id,card_id), UNIQUE(pack_id,position)
);
COMMIT;
