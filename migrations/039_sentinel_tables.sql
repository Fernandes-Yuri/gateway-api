-- =============================================================
-- MIGRACAO 039: Tabelas do sentinela (braga-sentinel, repo proprio)
-- Convenção: o DDL do sentinela vive aqui, no pipeline privilegiado do
-- gateway (MIGRATIONS_DATABASE_URL). O deploy do sentinela só faz DML
-- com a role limitada (bragasaude_app) e verifica as tabelas.
-- Espelho das migrations 001/002 do repo sentinela-bot.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS sentinel_events (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  nivel TEXT NOT NULL CHECK (nivel IN ('INFO','WARN','CRIT')),
  origem TEXT NOT NULL DEFAULT 'desconhecida',
  ip TEXT NOT NULL DEFAULT '-',
  rota TEXT NOT NULL DEFAULT '-',
  motivo TEXT NOT NULL DEFAULT '',
  detalhe TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sentinel_events_ts ON sentinel_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sentinel_events_ip ON sentinel_events (ip, created_at DESC);

CREATE TABLE IF NOT EXISTS sentinel_ip_state (
  ip TEXT PRIMARY KEY,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  strikes INT NOT NULL DEFAULT 0,
  blocked_until TIMESTAMPTZ NULL,
  motivo TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sentinel_daily_summary (
  dia DATE PRIMARY KEY,
  stats JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sentinel_actions (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tipo TEXT NOT NULL,
  alvo TEXT NOT NULL DEFAULT '-',
  motivo TEXT NOT NULL DEFAULT '',
  nivel TEXT NOT NULL CHECK (nivel IN ('toque','totp','totp_duplo')),
  status TEXT NOT NULL DEFAULT 'proposta'
    CHECK (status IN ('proposta','aguardando_totp','aprovada','negada','expirada','executada','falhou')),
  nonce TEXT UNIQUE,
  expires_at TIMESTAMPTZ,
  decided_at TIMESTAMPTZ,
  resultado TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sentinel_actions_status ON sentinel_actions (status, expires_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON
  sentinel_events, sentinel_ip_state, sentinel_daily_summary, sentinel_actions
  TO bragasaude_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bragasaude_app;

INSERT INTO schema_migrations(version) VALUES ('039_sentinel_tables') ON CONFLICT DO NOTHING;
COMMIT;
