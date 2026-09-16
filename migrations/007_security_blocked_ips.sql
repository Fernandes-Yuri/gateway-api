-- =============================================================
-- MIGRACAO 007: Bloqueio de IP por 24h e Registro de Violacoes de Seguranca
-- Armazena IPs que violaram rate limit (>3 vezes) ou tentaram
-- injecoes de prompt / ataques maliciosos na Gateway.
-- =============================================================

CREATE TABLE IF NOT EXISTS security_blocked_ips (
    ip VARCHAR(45) PRIMARY KEY,
    reason TEXT NOT NULL,
    violations INT NOT NULL DEFAULT 1,
    blocked_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_blocked_ips_until ON security_blocked_ips(blocked_until);

INSERT INTO schema_migrations(version) VALUES ('007_security_blocked_ips') ON CONFLICT DO NOTHING;
