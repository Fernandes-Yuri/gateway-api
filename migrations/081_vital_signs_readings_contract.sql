-- Evolui vital_signs.oxygen_saturation para DOUBLE PRECISION (preservando
-- leituras fracionadas como 98,5%) e garante confirmed_via canônico no schema.
SET lock_timeout = '5s';
SET statement_timeout = '120s';

ALTER TABLE vital_signs ADD COLUMN IF NOT EXISTS oxygen_saturation DOUBLE PRECISION;
ALTER TABLE vital_signs
    ALTER COLUMN oxygen_saturation TYPE DOUBLE PRECISION
    USING oxygen_saturation::double precision;

ALTER TABLE vital_signs ADD COLUMN IF NOT EXISTS confirmed_via VARCHAR(20) DEFAULT 'app';
ALTER TABLE vital_signs ADD COLUMN IF NOT EXISTS confirmation_code VARCHAR(20);
CREATE INDEX IF NOT EXISTS idx_vitals_confirmed_via ON vital_signs(confirmed_via);

CREATE OR REPLACE FUNCTION prepare_vital_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.user_id, 8042));
    IF EXISTS (SELECT 1 FROM vital_hydration_undos WHERE user_id=NEW.user_id AND vital_id=NEW.id) THEN
        NEW.hydration_ml := NULL;
        IF num_nonnulls(NEW.systolic_pressure, NEW.diastolic_pressure, NEW.heart_rate,
                       NEW.oxygen_saturation, NEW.glucose_level)=0 THEN
            NEW.deleted_at := COALESCE(NEW.deleted_at, now());
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION record_vital_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' OR to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
        INSERT INTO vital_sign_changes(user_id,vital_id,payload) VALUES(NEW.user_id,NEW.id,to_jsonb(NEW));
    END IF;
    RETURN NEW;
END $$;

INSERT INTO schema_migrations(version) VALUES ('081_vital_signs_readings_contract') ON CONFLICT DO NOTHING;
