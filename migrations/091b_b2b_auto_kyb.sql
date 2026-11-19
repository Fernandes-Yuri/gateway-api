-- Migration 091b (B2B-09B): Criterios de Cadastro B2B (CNPJ obrigatorio) & Motor Auto-KYB
-- Todas as alteracoes sao estritamente aditivas e reversiveis pelo script _rollback.sql.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

-- 1. Campos do responsavel, conselho profissional, localidade, telemetria Auto-KYB e anexos privados
ALTER TABLE partner_programs
    ADD COLUMN IF NOT EXISTS responsible_name varchar(160),
    ADD COLUMN IF NOT EXISTS responsible_cpf_hash varchar(64),
    ADD COLUMN IF NOT EXISTS responsible_cpf_masked varchar(20),
    ADD COLUMN IF NOT EXISTS responsible_role varchar(48),
    ADD COLUMN IF NOT EXISTS responsible_phone varchar(32),
    ADD COLUMN IF NOT EXISTS council_type varchar(16),
    ADD COLUMN IF NOT EXISTS council_number varchar(32),
    ADD COLUMN IF NOT EXISTS council_uf char(2),
    ADD COLUMN IF NOT EXISTS city varchar(96),
    ADD COLUMN IF NOT EXISTS state char(2),
    ADD COLUMN IF NOT EXISTS auto_kyb_status varchar(24),
    ADD COLUMN IF NOT EXISTS auto_kyb_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS auto_kyb_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS proof_documents jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 2. Constraints nomeadas e idempotentes para os novos campos
DO $$
DECLARE
    old_status_chk text;
BEGIN
    SELECT c.conname INTO old_status_chk
      FROM pg_constraint c
     WHERE c.conrelid = 'partner_programs'::regclass
       AND c.contype = 'c'
       AND pg_get_constraintdef(c.oid) LIKE '%approval_status%'
       AND c.conname <> 'chk_partner_programs_approval_status';

    IF old_status_chk IS NOT NULL THEN
        EXECUTE format('ALTER TABLE partner_programs DROP CONSTRAINT %I', old_status_chk);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_approval_status'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_approval_status
            CHECK (approval_status IN (
                'DRAFT',
                'PENDING_REVIEW',
                'CHANGES_REQUESTED',
                'APPROVED',
                'REJECTED',
                'SUSPENDED'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_responsible_role'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_responsible_role
            CHECK (responsible_role IS NULL OR responsible_role IN (
                'SOCIO_TITULAR',
                'RESPONSAVEL_TECNICO',
                'GERENTE_ADMIN'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_council_type'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_council_type
            CHECK (council_type IS NULL OR council_type IN (
                'CRM', 'CRN', 'CREFITO', 'COREN', 'CRF', 'CRP', 'CRO'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_auto_kyb_status'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_auto_kyb_status
            CHECK (auto_kyb_status IS NULL OR auto_kyb_status IN (
                'GREEN', 'YELLOW', 'RED', 'MANUAL_OVERRIDE'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_auto_kyb_flags_array'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_auto_kyb_flags_array
            CHECK (jsonb_typeof(auto_kyb_flags) = 'array');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_auto_kyb_evidence_object'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_auto_kyb_evidence_object
            CHECK (jsonb_typeof(auto_kyb_evidence) = 'object');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_partner_programs_proof_documents_array'
           AND conrelid = 'partner_programs'::regclass
    ) THEN
        ALTER TABLE partner_programs
            ADD CONSTRAINT chk_partner_programs_proof_documents_array
            CHECK (jsonb_typeof(proof_documents) = 'array');
    END IF;
END $$;

-- 3. Unicidade anti-abuso: apenas 1 programa ativo ou pendente por CNPJ
CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_programs_active_cnpj
    ON partner_programs (cnpj)
    WHERE cnpj IS NOT NULL AND approval_status <> 'REJECTED' AND closed_at IS NULL;

-- 4. Cache persistente de consultas CNPJ (TTL 24h) protegendo cotas da Receita Federal
CREATE TABLE IF NOT EXISTS cnpj_lookup_cache (
    cnpj_digits char(14) PRIMARY KEY,
    provider varchar(32) NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cnpj_lookup_cache_expires
    ON cnpj_lookup_cache (expires_at);

-- 5. Habilitar avisos de decisao B2B na central/outbox Push FCM (082)
CREATE OR REPLACE FUNCTION notification_source_allowed(
    k text,recipient text,target_type text,target_id text,patient text,actor text,event_key text
) RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM profiles p WHERE p.id=recipient
                  AND to_jsonb(p)->>'paused_at' IS NULL) THEN RETURN false; END IF;
    IF k='FAMILY_MESSAGE' THEN
        RETURN EXISTS(SELECT 1 FROM family_messages m WHERE m.id::text=substr(event_key,9)
            AND m.patient_id=patient AND m.sender_id=actor AND m.deleted_at IS NULL AND m.expires_at>now())
            AND (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'));
    ELSIF k LIKE 'CONSULTATION_%' THEN
        RETURN EXISTS(SELECT 1 FROM consultations c WHERE c.id::text=target_id AND c.user_id=patient)
            AND (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'));
    ELSIF k LIKE 'FAMILY_BINDING_%' THEN
        RETURN EXISTS(SELECT 1 FROM family_bindings b WHERE b.id::text=target_id
            AND recipient IN (b.patient_id,b.caregiver_id));
    ELSIF k='POST_REACTION' THEN
        RETURN recipient<>actor AND EXISTS(SELECT 1 FROM social_posts p
            JOIN post_reactions r ON r.post_id=p.id AND r.user_id=actor
            WHERE p.id::text=target_id AND p.user_id=recipient AND p.is_visible
              AND NOT EXISTS(SELECT 1 FROM social_user_blocks b
                WHERE (b.user_id=recipient AND b.blocked_user_id=actor)
                   OR (b.user_id=actor AND b.blocked_user_id=recipient))
              AND (p.visibility='PUBLIC' OR EXISTS(SELECT 1 FROM family_bindings b
                WHERE b.status='ACTIVE' AND ((b.patient_id=recipient AND b.caregiver_id=actor)
                    OR (b.patient_id=actor AND b.caregiver_id=recipient)))));
    ELSIF k IN ('MEDICATION_LOW_STOCK','MEDICATION_REMINDER','CARE_ALERT') THEN
        RETURN (recipient=patient OR EXISTS(SELECT 1 FROM family_bindings b
            WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE'))
            AND (target_type<>'medication' OR EXISTS(SELECT 1 FROM medications m
                WHERE m.id::text=target_id AND m.user_id=patient AND to_jsonb(m)->>'deleted_at' IS NULL));
    ELSIF k IN ('B2B_PROGRAM_APPROVED','B2B_PROGRAM_CHANGES_REQUESTED','B2B_PROGRAM_PENDING_REVIEW') THEN
        RETURN target_type='b2b_program' AND EXISTS(SELECT 1 FROM partner_programs pr
            WHERE pr.id::text=target_id AND pr.owner_user_id=recipient AND pr.closed_at IS NULL);
    END IF;
    RETURN false;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bragasaude_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON cnpj_lookup_cache
            TO bragasaude_app;
    END IF;
END $$;

ALTER TABLE account_storage_cleanup
    ADD COLUMN IF NOT EXISTS b2b_r2_keys jsonb NOT NULL DEFAULT '[]'::jsonb;
