-- Snapshots de integridade do conteúdo público de clinicas.bragasaude.online.
-- Registro interno autenticado; nenhuma superfície pública lê essas tabelas.
CREATE TABLE conteudo_snapshots (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    hash_sha256 CHAR(64) NOT NULL,
    body BYTEA NOT NULL,
    headers JSONB NOT NULL DEFAULT '{}',
    http_status INTEGER NOT NULL,
    match_state TEXT NOT NULL DEFAULT 'unknown' CHECK (match_state IN ('expected','unexpected','unknown')),
    tamanho INTEGER NOT NULL,
    coletado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS conteudo_snapshots_url_tempo ON conteudo_snapshots(url,coletado_em DESC);
DELETE FROM schema_migrations WHERE version='069_conteudo_snapshots';
INSERT INTO schema_migrations(version) VALUES ('069_conteudo_snapshots');
