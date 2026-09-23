-- 018_device_push_tokens_and_audit_schemas.sql
-- Higiene Fase B: persistencia relacional de tokens FCM e formalizacao de schemas previamente inline.
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Tabela canonica de tokens push FCM/APNS para cuidadores e usuarios
CREATE TABLE IF NOT EXISTS device_push_tokens (
    token text PRIMARY KEY,
    user_id varchar(128) NOT NULL,
    role text NOT NULL DEFAULT 'caregiver',
    platform text NOT NULL DEFAULT 'fcm',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_device_push_tokens_user ON device_push_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_device_push_tokens_active_role ON device_push_tokens(role, is_active);

-- Codigos de verificacao de autenticacao (formalizacao do antigo inline em server.py)
CREATE TABLE IF NOT EXISTS auth_verification_codes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    identifier VARCHAR(255) NOT NULL,
    code VARCHAR(10) NOT NULL,
    purpose VARCHAR(50) NOT NULL,
    channel VARCHAR(20) NOT NULL,
    attempts INT DEFAULT 0,
    is_used BOOLEAN DEFAULT FALSE,
    reset_token VARCHAR(255),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_auth_codes_lookup ON auth_verification_codes(identifier, purpose, is_used);
CREATE INDEX IF NOT EXISTS idx_auth_codes_token ON auth_verification_codes(reset_token);

-- Logs de auditoria (formalizacao do antigo inline em server.py)
CREATE TABLE IF NOT EXISTS audit_logs (
    id           VARCHAR(64)  PRIMARY KEY,
    user_id      VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    event_type   VARCHAR(100) NOT NULL,
    action       VARCHAR(100) NOT NULL,
    metadata     JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id, created_at DESC);

-- Preferencias do assistente e propostas pendentes (formalizacao do SCHEMA em assistant_preferences.py)
CREATE TABLE IF NOT EXISTS assistant_preferences (
    user_id VARCHAR(128) PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    preferences JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS assistant_preference_pending (
    user_id VARCHAR(128) PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL,
    operation JSONB NOT NULL,
    question TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '2 minutes'
);

INSERT INTO schema_migrations(version) VALUES ('018_device_push_tokens_and_audit_schemas') ON CONFLICT DO NOTHING;
COMMIT;
