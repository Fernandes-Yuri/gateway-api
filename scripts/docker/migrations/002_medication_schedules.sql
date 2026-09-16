-- Aplicar no banco existente antes de publicar o contrato medicationContract=2.
BEGIN;
ALTER TABLE medications ADD COLUMN IF NOT EXISTS schedule_times TEXT;
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS scheduled_for TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS medication_logs_one_per_dose
    ON medication_logs (user_id, medication_id, scheduled_for) WHERE scheduled_for IS NOT NULL;
-- Registros antigos permanecem sem scheduled_for: não inventar a dose histórica.
COMMIT;
