-- Aplicar no banco existente antes de publicar o contrato medicationContract=2.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE medications ADD COLUMN IF NOT EXISTS schedule_times TEXT;
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS scheduled_for TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS medication_logs_one_per_dose
    ON medication_logs (user_id, medication_id, scheduled_for) WHERE scheduled_for IS NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('003_medication_schedules') ON CONFLICT DO NOTHING;
COMMIT;
