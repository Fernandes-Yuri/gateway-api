-- DT-001: observação persistente, sem expurgo retroativo de perfis desconhecidos.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS account_auth_observations (
    user_id varchar(128) PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    observed_active_at timestamptz,
    missing_since timestamptz,
    last_checked_at timestamptz NOT NULL DEFAULT '-infinity',
    last_result text NOT NULL DEFAULT 'unconfirmed'
        CHECK(last_result IN ('unconfirmed', 'active', 'missing', 'unavailable', 'historical_unconfirmed'))
);
CREATE INDEX IF NOT EXISTS account_auth_observations_check_order
    ON account_auth_observations(last_checked_at, user_id);
ALTER TABLE account_storage_cleanup ADD COLUMN IF NOT EXISTS firebase_cleanup_required boolean NOT NULL DEFAULT false;
INSERT INTO schema_migrations(version) VALUES('056_firebase_account_reconciliation') ON CONFLICT DO NOTHING;
COMMIT;
