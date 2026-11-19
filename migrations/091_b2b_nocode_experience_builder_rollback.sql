DROP TRIGGER IF EXISTS trg_profiles_b2b_lgpd_cleanup ON profiles;
DROP FUNCTION IF EXISTS fn_profiles_b2b_lgpd_cleanup();

DROP INDEX IF EXISTS idx_program_enrollments_program_consented_id;

ALTER TABLE program_enrollments
    DROP COLUMN IF EXISTS muted_at,
    DROP COLUMN IF EXISTS family_share_enabled;

ALTER TABLE partner_programs
    DROP CONSTRAINT IF EXISTS chk_partner_programs_experience_config_object,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_template_key,
    DROP COLUMN IF EXISTS transfer_target_user_id,
    DROP COLUMN IF EXISTS closed_at,
    DROP COLUMN IF EXISTS ai_paused,
    DROP COLUMN IF EXISTS terms_version,
    DROP COLUMN IF EXISTS terms_accepted_at,
    DROP COLUMN IF EXISTS experience_config,
    DROP COLUMN IF EXISTS template_key;

DO $$
DECLARE
    fk_name text;
BEGIN
    IF to_regclass('partner_programs') IS NOT NULL THEN
        FOR fk_name IN
            SELECT c.conname
              FROM pg_constraint c
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
             WHERE c.conrelid = 'partner_programs'::regclass
               AND c.contype = 'f'
               AND a.attname = 'owner_user_id'
        LOOP
            EXECUTE format('ALTER TABLE partner_programs DROP CONSTRAINT IF EXISTS %I', fk_name);
        END LOOP;

        ALTER TABLE partner_programs
            ADD CONSTRAINT partner_programs_owner_user_id_fkey
            FOREIGN KEY (owner_user_id) REFERENCES profiles(id) ON DELETE RESTRICT;
    END IF;
END $$;
