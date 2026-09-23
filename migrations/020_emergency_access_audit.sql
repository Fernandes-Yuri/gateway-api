-- 020_emergency_access_audit.sql
-- Fase C: Auditoria completa de acesso à Ficha Médica de Resgate (LGPD) e last_accessed_at
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

-- Coluna de rastreamento do último acesso ao token de resgate
ALTER TABLE emergency_access_tokens
    ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz;

-- Trilha de auditoria formal para acessos públicos à ficha de resgate
CREATE TABLE IF NOT EXISTS emergency_access_audit (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_id uuid NOT NULL REFERENCES emergency_access_tokens(id) ON DELETE CASCADE,
    ip_address varchar(45),
    user_agent varchar(500),
    accessed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_emergency_access_audit_token ON emergency_access_audit(token_id, accessed_at DESC);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('020_emergency_access_audit', 'emergency_access_audit', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
