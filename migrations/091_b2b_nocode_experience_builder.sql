-- =============================================================
-- MIGRACAO 091 (B2B-15): Motor No-Code, Templates & Ciclo de Vida da Conta
--
-- Evolui aditivamente as tabelas B2B (criadas na 090):
--   1. partner_programs: template_key, experience_config (JSONB),
--      terms_accepted_at, terms_version, ai_paused, closed_at,
--      transfer_target_user_id e atualizacao da FK owner_user_id
--      (ON DELETE RESTRICT -> ON DELETE CASCADE) para que a exclusao
--      de conta LGPD (account_deletion.py) nunca falhe por constraint.
--   2. program_enrollments: family_share_enabled, muted_at e indice
--      composto de cursor (program_id, consented_at DESC, patient_user_id).
--   3. Trigger LGPD em profiles limpando actor_user_id/target_user_id
--      em program_audit_logs antes da exclusao da conta.
--
-- Nao altera tabelas, colunas ou contratos consumidos pelo app
-- em teste aberto (v1.9.6 build 43).
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. Colunas aditivas em partner_programs
ALTER TABLE partner_programs
    ADD COLUMN IF NOT EXISTS template_key varchar(40) NOT NULL DEFAULT 'CLINICA_GERAL',
    ADD COLUMN IF NOT EXISTS experience_config jsonb NOT NULL DEFAULT '{
        "schema_version": 1,
        "template_key": "CLINICA_GERAL",
        "welcome_headline": "Seu espaço de acompanhamento entre consultas",
        "welcome_message": "Organize passos, hidratação e registros de rotina para compartilhar apenas o que desejar antes do retorno.",
        "enabled_metrics": ["steps", "hydration", "medications", "authorized_records"],
        "features": {
            "assistant_chat_enabled": true,
            "direct_partner_chat_enabled": false,
            "ai_receptionist_enabled": false,
            "family_link_enabled": false,
            "partner_reminders_enabled": true,
            "appointments_enabled": true,
            "pre_consultation_pdf_enabled": true
        },
        "direct_chat_policy": {
            "service_hours_label": "Seg a Sex, 08h às 18h",
            "response_sla_label": "Resposta em até 1 dia útil",
            "allow_off_hours_queue": true
        },
        "ai_receptionist_policy": {
            "persona_name": "Assistente Virtual do Programa",
            "activation_mode": "OUT_OF_HOURS_ONLY",
            "greeting_message": "Olá! Sou a recepcionista virtual deste programa. Posso tirar dúvidas sobre horários, funcionamento e como enviar seu resumo — ou chamar o responsável quando precisar.",
            "partner_faq_knowledge": "",
            "auto_escalate_to_human_on_unknown": true
        },
        "appointments_policy": {
            "allowed_modalities": ["IN_PERSON", "REMOTE"],
            "booking_mode": "REQUEST_CONFIRMATION",
            "availabilities_summary": "Terças e Quintas, 09h às 17h",
            "external_booking_url": null
        }
    }'::jsonb,
    ADD COLUMN IF NOT EXISTS terms_accepted_at timestamptz,
    ADD COLUMN IF NOT EXISTS terms_version varchar(32),
    ADD COLUMN IF NOT EXISTS ai_paused boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz,
    ADD COLUMN IF NOT EXISTS transfer_target_user_id varchar(128) REFERENCES profiles(id) ON DELETE SET NULL;

DO $$
DECLARE
    fk_name text;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'partner_programs'::regclass
           AND conname = 'chk_partner_programs_template_key'
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_template_key
            CHECK (template_key IN (
                'CLINICA_GERAL',
                'NUTRICAO_BEM_ESTAR',
                'FISIO_REABILITACAO',
                'CUIDADO_SENIOR_HOMECARE',
                'FARMACIA_ADESAO',
                'SAUDE_CORPORATIVA',
                'CUSTOM'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'partner_programs'::regclass
           AND conname = 'chk_partner_programs_experience_config_object'
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_experience_config_object
            CHECK (jsonb_typeof(experience_config) = 'object');
    END IF;

    -- Substitui ON DELETE RESTRICT (090:19) por ON DELETE CASCADE em owner_user_id
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
        FOREIGN KEY (owner_user_id) REFERENCES profiles(id) ON DELETE CASCADE;
END $$;

-- 2. Colunas aditivas e indice de cursor em program_enrollments
ALTER TABLE program_enrollments
    ADD COLUMN IF NOT EXISTS family_share_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS muted_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_program_enrollments_program_consented_id
    ON program_enrollments (program_id, consented_at DESC, patient_user_id)
    WHERE status = 'ACTIVE';

-- 3. Trigger LGPD: anonimiza actor_user_id/target_user_id em program_audit_logs ao excluir perfil
CREATE OR REPLACE FUNCTION fn_profiles_b2b_lgpd_cleanup()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE program_audit_logs
       SET actor_user_id = CASE WHEN actor_user_id = OLD.id THEN NULL ELSE actor_user_id END,
           target_user_id = CASE WHEN target_user_id = OLD.id THEN NULL ELSE target_user_id END
     WHERE actor_user_id = OLD.id OR target_user_id = OLD.id;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_profiles_b2b_lgpd_cleanup ON profiles;
CREATE TRIGGER trg_profiles_b2b_lgpd_cleanup
    BEFORE DELETE ON profiles
    FOR EACH ROW
    EXECUTE FUNCTION fn_profiles_b2b_lgpd_cleanup();
