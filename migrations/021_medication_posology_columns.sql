-- 021_medication_posology_columns.sql
-- Ciclo de vida do medicamento: posologia inteligente, horários flexíveis e contexto de refeição
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

ALTER TABLE medications
    ADD COLUMN IF NOT EXISTS frequency_interval_hours integer,
    ADD COLUMN IF NOT EXISTS treatment_duration_days integer,
    ADD COLUMN IF NOT EXISTS meal_context varchar(50);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('021_medication_posology_columns', 'medication_posology_columns', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
