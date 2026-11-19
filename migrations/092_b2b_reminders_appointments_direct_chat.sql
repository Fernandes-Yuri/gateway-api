-- =============================================================
-- MIGRACAO 092 (B2B-18/19/20/20B/21B): Lembretes do Parceiro,
-- Agendamentos In-App, Chat Direto / Recepcionista IA & UGC
--
-- Estritamente aditiva e reversivel:
--   1. program_reminders + program_reminder_receipts (B2B-18)
--      -> Nunca escreve em medications / medication_logs.
--   2. program_appointments (B2B-19)
--      -> Isolada da tabela consultations (Care OS pessoal).
--   3. program_direct_threads + program_direct_messages (B2B-20/20B)
--      -> Buffer temporario em Rust (isolado de family_messages).
--   4. program_abuse_reports (B2B-21B)
--      -> Canal de denuncia UGC Play Store.
--   5. Atualiza notification_source_allowed (082/091b) para
--      habilitar Push FCM em PARTNER_REMINDER, PARTNER_APPOINTMENT
--      e PARTNER_DIRECT_MESSAGE sem PHI no payload.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. Lembretes e orientacoes de rotina do programa parceiro (B2B-18)
CREATE TABLE IF NOT EXISTS program_reminders (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    target_patient_id varchar(128) NULL REFERENCES profiles(id) ON DELETE CASCADE,
    target_category varchar(32) NULL
        CHECK (target_category IS NULL OR target_category IN ('steps', 'hydration', 'medications', 'authorized_records')),
    reminder_type varchar(24) NOT NULL DEFAULT 'ROUTINE'
        CHECK (reminder_type IN ('ROUTINE', 'HYDRATION', 'ACTIVITY', 'PRE_CONSULT')),
    title varchar(140) NOT NULL CHECK (char_length(trim(title)) > 0),
    body varchar(500) NOT NULL CHECK (char_length(trim(body)) > 0),
    created_by varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '45 days')
);

CREATE INDEX IF NOT EXISTS idx_program_reminders_program_created
    ON program_reminders (program_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_program_reminders_patient_created
    ON program_reminders (target_patient_id, created_at DESC)
    WHERE target_patient_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS program_reminder_receipts (
    reminder_id uuid NOT NULL REFERENCES program_reminders(id) ON DELETE CASCADE,
    patient_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_by_uid varchar(128) NULL REFERENCES profiles(id) ON DELETE SET NULL,
    PRIMARY KEY (reminder_id, patient_user_id)
);

-- 2. Agendamento configuravel do programa (B2B-19 — separado de consultations)
CREATE TABLE IF NOT EXISTS program_appointments (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    patient_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    modality varchar(24) NOT NULL CHECK (modality IN ('IN_PERSON', 'REMOTE')),
    requested_slot_text varchar(160) NOT NULL CHECK (char_length(trim(requested_slot_text)) > 0),
    scheduled_at timestamptz NULL,
    patient_note varchar(400) NULL,
    partner_note varchar(400) NULL,
    status varchar(32) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CONFIRMED', 'RESCHEDULE_REQUESTED', 'CANCELLED', 'COMPLETED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_program_appointments_program_status_updated
    ON program_appointments (program_id, status, updated_at DESC);

-- 3. Threads (handoff IA <-> Humano B2B-20B) & mensagens diretas (buffer B2B-20)
CREATE TABLE IF NOT EXISTS program_direct_threads (
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    patient_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    handling_mode varchar(24) NOT NULL DEFAULT 'AI_ACTIVE'
        CHECK (handling_mode IN ('AI_ACTIVE', 'HUMAN_ACTIVE', 'WAITING_HUMAN')),
    taken_over_by varchar(128) NULL REFERENCES profiles(id) ON DELETE SET NULL,
    taken_over_at timestamptz NULL,
    handoff_note varchar(240) NULL,
    ai_turns_today integer NOT NULL DEFAULT 0,
    ai_turns_reset_date date NOT NULL DEFAULT CURRENT_DATE,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, patient_user_id)
);

CREATE TABLE IF NOT EXISTS program_direct_messages (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    patient_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    sender_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    sender_role varchar(24) NOT NULL CHECK (sender_role IN ('PARTNER', 'PATIENT', 'AI_RECEPTIONIST')),
    body varchar(1000) NOT NULL CHECK (char_length(trim(body)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz NULL
);

CREATE INDEX IF NOT EXISTS idx_program_direct_messages_thread_created
    ON program_direct_messages (program_id, patient_user_id, created_at DESC);

-- 4. Denuncias / moderacao UGC de programas parceiros (B2B-21B Play Store)
CREATE TABLE IF NOT EXISTS program_abuse_reports (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    program_id uuid NOT NULL REFERENCES partner_programs(id) ON DELETE CASCADE,
    reporter_user_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reason varchar(64) NOT NULL CHECK (char_length(trim(reason)) > 0),
    details varchar(500) NULL,
    status varchar(24) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'REVIEWED', 'DISMISSED')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_program_abuse_reports_program_created
    ON program_abuse_reports (program_id, created_at DESC);

-- 5. Habilitar Push FCM via Central de Notificacoes (082) para engajamento B2B
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
    ELSIF k='PARTNER_REMINDER' THEN
        RETURN target_type='b2b_reminder' AND EXISTS(
            SELECT 1 FROM program_reminders r
            JOIN partner_programs pr ON pr.id=r.program_id
            JOIN program_enrollments e ON e.program_id=pr.id
                AND e.patient_user_id=patient
                AND e.status='ACTIVE'
                AND e.muted_at IS NULL
            WHERE r.id::text=target_id
              AND pr.closed_at IS NULL
              AND pr.approval_status='APPROVED'
              AND (r.target_patient_id IS NULL OR r.target_patient_id=patient)
              AND (recipient=patient OR (
                    e.family_share_enabled
                    AND COALESCE((pr.experience_config->'features'->>'family_link_enabled')::boolean, false)
                    AND EXISTS(SELECT 1 FROM family_bindings b
                        WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE')
              ))
        );
    ELSIF k='PARTNER_APPOINTMENT' THEN
        RETURN target_type='b2b_appointment' AND EXISTS(
            SELECT 1 FROM program_appointments a
            JOIN partner_programs pr ON pr.id=a.program_id
            JOIN program_enrollments e ON e.program_id=a.program_id
                AND e.patient_user_id=a.patient_user_id
                AND e.status='ACTIVE'
            WHERE a.id::text=target_id
              AND a.patient_user_id=patient
              AND pr.closed_at IS NULL
              AND pr.approval_status='APPROVED'
              AND (
                    recipient=pr.owner_user_id
                    OR (recipient=patient AND e.muted_at IS NULL)
                    OR (
                        e.family_share_enabled
                        AND COALESCE((pr.experience_config->'features'->>'family_link_enabled')::boolean, false)
                        AND EXISTS(SELECT 1 FROM family_bindings b
                            WHERE b.patient_id=patient AND b.caregiver_id=recipient AND b.status='ACTIVE')
                    )
              )
        );
    ELSIF k='PARTNER_DIRECT_MESSAGE' THEN
        RETURN target_type='b2b_direct_thread' AND EXISTS(
            SELECT 1 FROM partner_programs pr
            JOIN program_enrollments e ON e.program_id=pr.id
                AND e.patient_user_id=patient
                AND e.status='ACTIVE'
            WHERE pr.id::text=target_id
              AND pr.closed_at IS NULL
              AND pr.approval_status='APPROVED'
              AND (recipient=pr.owner_user_id OR (recipient=patient AND e.muted_at IS NULL))
        );
    END IF;
    RETURN false;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bragasaude_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON program_reminders,
               program_reminder_receipts,
               program_appointments,
               program_direct_threads,
               program_direct_messages,
               program_abuse_reports
            TO bragasaude_app;
    END IF;
END $$;
