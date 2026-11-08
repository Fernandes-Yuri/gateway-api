SET lock_timeout = '5s';
SET statement_timeout = '120s';

ALTER TABLE vital_signs ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE TABLE IF NOT EXISTS vital_hydration_undos (
    user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    vital_id uuid NOT NULL,
    undone_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id, vital_id)
);

CREATE TABLE IF NOT EXISTS vital_sign_changes (
    revision bigserial PRIMARY KEY,
    user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    vital_id uuid NOT NULL,
    payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS vital_changes_owner_revision ON vital_sign_changes(user_id, revision);

-- Serialize revisions for one patient before sequence allocation, so a reader
-- cannot advance past an uncommitted earlier change for the same patient.
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
DROP TRIGGER IF EXISTS prepare_vital_change ON vital_signs;
CREATE TRIGGER prepare_vital_change BEFORE INSERT OR UPDATE ON vital_signs FOR EACH ROW EXECUTE FUNCTION prepare_vital_change();
DROP TRIGGER IF EXISTS record_vital_change ON vital_signs;
CREATE TRIGGER record_vital_change AFTER INSERT OR UPDATE ON vital_signs FOR EACH ROW EXECUTE FUNCTION record_vital_change();

-- Seed an existing database once; retries of the migration do not duplicate it.
INSERT INTO vital_sign_changes(user_id,vital_id,payload)
SELECT v.user_id,v.id,to_jsonb(v) FROM vital_signs v
WHERE NOT EXISTS (SELECT 1 FROM vital_sign_changes c WHERE c.vital_id=v.id);

CREATE TABLE IF NOT EXISTS consultations (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    caregiver_id varchar(128) REFERENCES profiles(id) ON DELETE SET NULL,
    caregiver_name text,
    caregiver_relation text,
    title text NOT NULL,
    scheduled_date timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'SUGGESTED',
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE consultations ADD COLUMN IF NOT EXISTS created_by varchar(128) REFERENCES profiles(id) ON DELETE SET NULL;
ALTER TABLE consultations ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
UPDATE consultations SET created_by=user_id WHERE created_by IS NULL;
CREATE INDEX IF NOT EXISTS consultations_patient_date ON consultations(user_id,scheduled_date,id);
