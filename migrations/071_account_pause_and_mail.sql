BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS paused_at timestamptz;
CREATE TABLE IF NOT EXISTS account_lifecycle_mail (
    event_key text PRIMARY KEY,
    user_id varchar(128) NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('paused', 'deleted')),
    to_email varchar(254) NOT NULL,
    user_name text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT NOW(),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_account_lifecycle_mail_due ON account_lifecycle_mail(next_attempt_at);
ALTER TABLE user_deletion_logs ADD COLUMN IF NOT EXISTS confirmation_sent_at timestamptz;
INSERT INTO schema_migrations(version) VALUES ('071_account_pause_and_mail') ON CONFLICT DO NOTHING;
COMMIT;
