-- Apply using psql -v ON_ERROR_STOP=1. Existing tables and data are preserved.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS basic_profile_complete boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS self_care_complete boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS self_care_setup_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS diabetes_type text,
    ADD COLUMN IF NOT EXISTS food_allergies text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS custom_food_restrictions text,
    ADD COLUMN IF NOT EXISTS activity_level text;
ALTER TABLE profiles ALTER COLUMN user_role DROP DEFAULT;
ALTER TABLE profiles ALTER COLUMN daily_calorie_target TYPE double precision;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='profiles_mode_check' AND conrelid='profiles'::regclass) THEN
        ALTER TABLE profiles ADD CONSTRAINT profiles_mode_check CHECK ((
            (user_role IS NULL AND caregiver_mode IS NULL) OR
            (user_role = 'PATIENT' AND caregiver_mode IS NULL) OR
            (user_role = 'CAREGIVER' AND (caregiver_mode IS NULL OR caregiver_mode IN ('VIEWER_ONLY', 'HYBRID')))
        ) IS TRUE) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='profiles_completion_check' AND conrelid='profiles'::regclass) THEN
        ALTER TABLE profiles ADD CONSTRAINT profiles_completion_check CHECK ((
            (NOT basic_profile_complete OR (btrim(full_name) <> '' AND
                (user_role = 'PATIENT' OR (user_role='CAREGIVER' AND caregiver_mode IS NOT NULL))))
            AND (NOT self_care_complete OR (basic_profile_complete AND
                (user_role='PATIENT' OR caregiver_mode='HYBRID') AND
                height BETWEEN 50 AND 250 AND weight BETWEEN 20 AND 350))
            AND (NOT self_care_setup_pending OR (user_role='CAREGIVER' AND caregiver_mode='VIEWER_ONLY'))
        ) IS TRUE) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='family_binding_people_check' AND conrelid='family_bindings'::regclass) THEN
        ALTER TABLE family_bindings ADD CONSTRAINT family_binding_people_check CHECK (
            caregiver_id IS NULL OR caregiver_id <> patient_id
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='family_binding_status_check' AND conrelid='family_bindings'::regclass) THEN
        -- Keep caregiver_id nullable: its FK uses ON DELETE SET NULL.
        ALTER TABLE family_bindings ADD CONSTRAINT family_binding_status_check CHECK (
            status IS NOT NULL AND status IN ('PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED')
        ) NOT VALID;
    END IF;
END $$;
INSERT INTO schema_migrations(version) VALUES ('001_profile_and_family') ON CONFLICT DO NOTHING;
COMMIT;
