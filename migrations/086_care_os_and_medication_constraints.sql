-- =============================================================
-- MIGRACAO 086: Constraints de negocio de farmacia, sintomas e consultas
-- Espelha validacoes de schemas/medications.py, schemas/clinical.py
-- e routers/consultations.py com NOT VALID.
-- =============================================================

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'medications_dosage_mg_range') THEN
        ALTER TABLE medications
            ADD CONSTRAINT medications_dosage_mg_range
            CHECK (dosage_mg IS NULL OR (dosage_mg > 0 AND dosage_mg <= 100000)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'medications_frequency_hours_range') THEN
        ALTER TABLE medications
            ADD CONSTRAINT medications_frequency_hours_range
            CHECK (frequency_interval_hours IS NULL OR (frequency_interval_hours BETWEEN 1 AND 48)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'medications_duration_days_range') THEN
        ALTER TABLE medications
            ADD CONSTRAINT medications_duration_days_range
            CHECK (treatment_duration_days IS NULL OR (treatment_duration_days BETWEEN 1 AND 365)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'medications_ean_barcode_format') THEN
        ALTER TABLE medications
            ADD CONSTRAINT medications_ean_barcode_format
            CHECK (ean_barcode IS NULL OR ean_barcode ~ '^[0-9]{13}$') NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symptoms_diary_sleep_quality_range') THEN
        ALTER TABLE symptoms_diary
            ADD CONSTRAINT symptoms_diary_sleep_quality_range
            CHECK (sleep_quality IS NULL OR (sleep_quality BETWEEN 1 AND 5)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symptoms_diary_disposition_range') THEN
        ALTER TABLE symptoms_diary
            ADD CONSTRAINT symptoms_diary_disposition_range
            CHECK (disposition IS NULL OR (disposition BETWEEN 1 AND 5)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symptoms_diary_input_method_enum') THEN
        ALTER TABLE symptoms_diary
            ADD CONSTRAINT symptoms_diary_input_method_enum
            CHECK (input_method IN ('VOICE', 'TEXT')) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symptoms_diary_text_length') THEN
        ALTER TABLE symptoms_diary
            ADD CONSTRAINT symptoms_diary_text_length
            CHECK (char_length(symptoms_text) BETWEEN 1 AND 1000) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'consultations_status_enum') THEN
        ALTER TABLE consultations
            ADD CONSTRAINT consultations_status_enum
            CHECK (status IN ('SUGGESTED', 'ACCEPTED', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'COMPLETED')) NOT VALID;
    END IF;
END $$;
