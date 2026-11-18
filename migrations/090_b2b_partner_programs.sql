-- =============================================================
-- MIGRACAO 090 (B2B-03): Modelagem minima multi-tenant & auditoria
--
-- Cria as tabelas aditivas do piloto B2B (Programas Parceiros In-App):
--   1. partner_programs
--   2. program_enrollments
--   3. program_audit_logs
--
-- Nao altera tabelas, colunas ou contratos consumidos pelo app
-- em teste aberto (v1.9.6 build 43).
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. Programas de parceiros (1 conta responsavel por programa no piloto)
CREATE TABLE IF NOT EXISTS partner_programs (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_user_id varchar(128) NOT NULL UNIQUE REFERENCES profiles(id) ON DELETE RESTRICT,
    program_name varchar(120) NOT NULL,
    company_name varchar(160) NOT NULL,
    cnpj varchar(18),
    primary_color varchar(7) NOT NULL DEFAULT '#1A6B5C'
        CHECK (primary_color ~ '^#[0-9A-Fa-f]{6}$'),
    logo_url text,
    approval_status varchar(32) NOT NULL DEFAULT 'DRAFT'
        CHECK (approval_status IN (
            'DRAFT',
            'PENDING_REVIEW',
            'CHANGES_REQUESTED',
            'APPROVED',
            'REJECTED'
        )),
    review_note text,
    reviewed_at timestamptz,
    reviewed_by varchar(128),
    invite_code varchar(32) UNIQUE,
    invite_paused boolean NOT NULL DEFAULT false,
    seat_limit integer NOT NULL DEFAULT 10 CHECK (seat_limit >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_partner_programs_status_created
    ON partner_programs (approval_status, created_at DESC);

-- 2. Vinculos paciente-programa e consentimento granular explicito
CREATE TABLE IF NOT EXISTS program_enrollments (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    patient_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    status varchar(24) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'REVOKED', 'REMOVED')),
    shared_categories jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(shared_categories) = 'array'),
    consented_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (program_id, patient_user_id)
);

CREATE INDEX IF NOT EXISTS idx_program_enrollments_active_program
    ON program_enrollments (program_id)
    WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_program_enrollments_patient
    ON program_enrollments (patient_user_id, status);

-- 3. Trilha de auditoria B2B (append-only)
CREATE TABLE IF NOT EXISTS program_audit_logs (
    id bigserial PRIMARY KEY,
    program_id uuid REFERENCES partner_programs(id) ON DELETE SET NULL,
    actor_user_id varchar(128),
    target_user_id varchar(128),
    action varchar(64) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_program_audit_logs_program_created
    ON program_audit_logs (program_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_program_audit_logs_target_created
    ON program_audit_logs (target_user_id, created_at DESC);

-- 4. Permissoes da role de runtime (quando presente no ambiente)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bragasaude_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON partner_programs, program_enrollments
            TO bragasaude_app;
        GRANT SELECT, INSERT
            ON program_audit_logs
            TO bragasaude_app;
        GRANT USAGE, SELECT
            ON SEQUENCE program_audit_logs_id_seq
            TO bragasaude_app;
    END IF;
END $$;
