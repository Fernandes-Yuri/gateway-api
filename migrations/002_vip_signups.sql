-- Existing PostgreSQL database; no Firebase Auth accounts or clinical tables changed.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vip_signups (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL CHECK (length(btrim(full_name)) BETWEEN 1 AND 255),
    email text NOT NULL UNIQUE CHECK (email = lower(btrim(email)) AND length(email) BETWEEN 3 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'PENDING',
    source text NOT NULL DEFAULT 'onboarding-site'
);
INSERT INTO schema_migrations(version) VALUES ('002_vip_signups') ON CONFLICT DO NOTHING;
COMMIT;
