-- =============================================================
-- MIGRACAO 087: Integridade de vinculos familiares, feed social e perfis
-- Usa NOT VALID para compatibilidade com registros historicos.
-- =============================================================

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'family_bindings_no_self_caregiver') THEN
        ALTER TABLE family_bindings
            ADD CONSTRAINT family_bindings_no_self_caregiver
            CHECK (caregiver_id IS NULL OR patient_id <> caregiver_id) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'family_bindings_status_enum') THEN
        ALTER TABLE family_bindings
            ADD CONSTRAINT family_bindings_status_enum
            CHECK (status IN ('PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'REJECTED')) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'social_posts_visibility_enum') THEN
        ALTER TABLE social_posts
            ADD CONSTRAINT social_posts_visibility_enum
            CHECK (visibility IN ('PUBLIC', 'FAMILY', 'PRIVATE')) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_user_role_enum') THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_user_role_enum
            CHECK (user_role IS NULL OR user_role IN ('PATIENT', 'CAREGIVER')) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_sleep_start_format') THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_sleep_start_format
            CHECK (sleep_start_time IS NULL OR sleep_start_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_sleep_end_format') THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_sleep_end_format
            CHECK (sleep_end_time IS NULL OR sleep_end_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$') NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_hydration_target_range') THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_hydration_target_range
            CHECK (hydration_target_ml IS NULL OR (hydration_target_ml BETWEEN 500 AND 10000)) NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_step_goal_range') THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_step_goal_range
            CHECK (step_goal IS NULL OR (step_goal BETWEEN 1000 AND 100000)) NOT VALID;
    END IF;
END $$;
