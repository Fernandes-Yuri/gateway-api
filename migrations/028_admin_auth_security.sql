-- Estado compartilhado entre réplicas para revogação, limites e uso único.
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS admin_auth_limits (
    key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_auth_limits_expiry ON admin_auth_limits(expires_at);

CREATE TABLE IF NOT EXISTS admin_master_uses (
    fingerprint TEXT NOT NULL,
    step BIGINT NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (fingerprint, step)
);

CREATE TABLE IF NOT EXISTS admin_challenges (
    id TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'register', 'reset')),
    answer INTEGER NOT NULL CHECK (answer BETWEEN 0 AND 260),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_challenges_expiry ON admin_challenges(expires_at);
