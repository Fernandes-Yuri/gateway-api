-- Rollback da Migration 091b (B2B-09B): Auto-KYB e Cache CNPJ

SET lock_timeout = '5s';
SET statement_timeout = '120s';

DROP INDEX IF EXISTS uq_partner_programs_active_cnpj;
DROP INDEX IF EXISTS idx_cnpj_lookup_cache_expires;
DROP TABLE IF EXISTS cnpj_lookup_cache;

-- Restaura definicao anterior de notification_source_allowed (082)
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
    END IF;
    RETURN false;
END $$;

UPDATE partner_programs
   SET approval_status = 'CHANGES_REQUESTED'
 WHERE approval_status = 'SUSPENDED';

ALTER TABLE partner_programs
    DROP CONSTRAINT IF EXISTS chk_partner_programs_approval_status,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_responsible_role,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_council_type,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_auto_kyb_status,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_auto_kyb_flags_array,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_auto_kyb_evidence_object,
    DROP CONSTRAINT IF EXISTS chk_partner_programs_proof_documents_array;

ALTER TABLE partner_programs
    ADD CONSTRAINT partner_programs_approval_status_check
    CHECK (approval_status IN (
        'DRAFT',
        'PENDING_REVIEW',
        'CHANGES_REQUESTED',
        'APPROVED',
        'REJECTED'
    ));

ALTER TABLE account_storage_cleanup
    DROP COLUMN IF EXISTS b2b_r2_keys;

ALTER TABLE partner_programs
    DROP COLUMN IF EXISTS responsible_name,
    DROP COLUMN IF EXISTS responsible_cpf_hash,
    DROP COLUMN IF EXISTS responsible_cpf_masked,
    DROP COLUMN IF EXISTS responsible_role,
    DROP COLUMN IF EXISTS responsible_phone,
    DROP COLUMN IF EXISTS council_type,
    DROP COLUMN IF EXISTS council_number,
    DROP COLUMN IF EXISTS council_uf,
    DROP COLUMN IF EXISTS city,
    DROP COLUMN IF EXISTS state,
    DROP COLUMN IF EXISTS auto_kyb_status,
    DROP COLUMN IF EXISTS auto_kyb_flags,
    DROP COLUMN IF EXISTS auto_kyb_evidence,
    DROP COLUMN IF EXISTS proof_documents;
