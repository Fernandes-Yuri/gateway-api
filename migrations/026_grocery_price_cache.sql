-- Verifica existência e cria tabela de cache de preços de alimentos
CREATE TABLE IF NOT EXISTS grocery_price_cache (
    food_name       TEXT PRIMARY KEY,
    search_term     TEXT NOT NULL,
    price_avg       NUMERIC(10,2),
    price_min       NUMERIC(10,2),
    price_max       NUMERIC(10,2),
    source          TEXT NOT NULL DEFAULT 'meli',
    results_count   INT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    needs_manual    BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE grocery_price_cache IS 'Cache de preços médios de alimentos consultados na API do Mercado Livre. needs_manual=TRUE indica item sem preço que precisa de inserção manual pelo admin.';

INSERT INTO schema_migrations (version, name) VALUES ('026', 'grocery_price_cache') ON CONFLICT DO NOTHING;
