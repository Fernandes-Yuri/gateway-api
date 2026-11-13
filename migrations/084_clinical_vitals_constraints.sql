-- =============================================================
-- MIGRACAO 084: Constraints de integridade clinica em vital_signs e biometrics
-- Espelha no PostgreSQL as validacoes de faixa fisiologica do backend
-- (routers/sync.py e services/biometry.py).
--
-- Usa NOT VALID para evitar varredura bloqueante e nao quebrar em
-- registros historicos legados; novos INSERT/UPDATE sao validados
-- imediatamente. VALIDATE CONSTRAINT sera executado apos auditoria.
-- =============================================================

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_systolic_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_systolic_range
            CHECK (systolic_pressure IS NULL OR (systolic_pressure BETWEEN 40 AND 300)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_diastolic_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_diastolic_range
            CHECK (diastolic_pressure IS NULL OR (diastolic_pressure BETWEEN 20 AND 200)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_heart_rate_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_heart_rate_range
            CHECK (heart_rate IS NULL OR (heart_rate BETWEEN 20 AND 250)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_oxygen_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_oxygen_range
            CHECK (oxygen_saturation IS NULL OR (oxygen_saturation >= 30.0 AND oxygen_saturation <= 100.0)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_glucose_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_glucose_range
            CHECK (glucose_level IS NULL OR (glucose_level BETWEEN 10 AND 1000)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_hydration_range') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_hydration_range
            CHECK (hydration_ml IS NULL OR (hydration_ml > 0 AND hydration_ml <= 20000)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vital_signs_at_least_one_metric') THEN
        ALTER TABLE vital_signs
            ADD CONSTRAINT vital_signs_at_least_one_metric
            CHECK (
                deleted_at IS NOT NULL
                OR num_nonnulls(
                    systolic_pressure,
                    diastolic_pressure,
                    heart_rate,
                    oxygen_saturation,
                    glucose_level,
                    hydration_ml
                ) > 0
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'biometrics_weight_range') THEN
        ALTER TABLE biometrics
            ADD CONSTRAINT biometrics_weight_range
            CHECK (weight_kg > 0 AND weight_kg <= 500) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'biometrics_height_range') THEN
        ALTER TABLE biometrics
            ADD CONSTRAINT biometrics_height_range
            CHECK (height_m IS NULL OR (height_m >= 0.30 AND height_m <= 2.50)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'biometrics_imc_range') THEN
        ALTER TABLE biometrics
            ADD CONSTRAINT biometrics_imc_range
            CHECK (imc IS NULL OR (imc >= 5.0 AND imc <= 150.0)) NOT VALID;
    END IF;
END $$;
