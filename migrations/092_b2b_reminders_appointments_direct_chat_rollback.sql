-- Rollback da Migration 092: Lembretes, Agendamentos, Chat Direto B2B & UGC

SET lock_timeout = '5s';
SET statement_timeout = '120s';

-- Restaura definicao anterior de notification_source_allowed (091b)
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

DROP TABLE IF EXISTS program_abuse_reports;
DROP TABLE IF EXISTS program_direct_messages;
DROP TABLE IF EXISTS program_direct_threads;
DROP TABLE IF EXISTS program_appointments;
DROP TABLE IF EXISTS program_reminder_receipts;
DROP TABLE IF EXISTS program_reminders;
