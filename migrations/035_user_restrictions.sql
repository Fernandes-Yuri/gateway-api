BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS session_version integer NOT NULL DEFAULT 0;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS session_revoked_at timestamptz;
CREATE TABLE IF NOT EXISTS user_restrictions (
 user_id varchar(128) PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
 level text NOT NULL CHECK (level IN ('read-only','no-sync','suspended')),
 reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
 expires_at timestamptz,
 created_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_restrictions_expiry ON user_restrictions(expires_at);
CREATE TABLE IF NOT EXISTS moderation_audit_log (
 id bigserial PRIMARY KEY, actor_email text NOT NULL, ip text NOT NULL,
 target_uid varchar(128), target_ip text, action text NOT NULL,
 reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((target_uid IS NULL) <> (target_ip IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_moderation_uid ON moderation_audit_log(target_uid,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_moderation_ip ON moderation_audit_log(target_ip,created_at DESC);
INSERT INTO schema_migrations(version) VALUES ('035_user_restrictions') ON CONFLICT DO NOTHING;
COMMIT;
