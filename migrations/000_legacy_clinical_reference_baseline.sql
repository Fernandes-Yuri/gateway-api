-- Baseline histórico necessário para reexecutar a migration 016 em bancos novos.
-- A migration 044 remove esta tabela definitivamente. Nenhuma rota a consome.
CREATE TABLE IF NOT EXISTS clinical_references (
    item_key VARCHAR(100) PRIMARY KEY,
    item_name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    unit VARCHAR(50),
    min_target DOUBLE PRECISION,
    max_target DOUBLE PRECISION,
    min_critical DOUBLE PRECISION,
    max_critical DOUBLE PRECISION,
    interpretation_hint TEXT,
    institution VARCHAR(255),
    document_version VARCHAR(255),
    parameter VARCHAR(255),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
