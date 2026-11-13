-- =============================================================
-- MIGRACAO 085: Foreign Keys faltantes com blindagem NOT VALID
-- Nota arquitetural:
--   - vital_hydration_undos.vital_id NAO recebe FK pois aceita tombstones
--     offline-first antes do upload de vital_signs.
--   - account_storage_cleanup e account_lifecycle_mail NAO recebem FK
--     ON DELETE CASCADE pois precisam sobreviver ao DELETE FROM profiles
--     para concluir limpeza de S3/Firebase e envio de e-mail LGPD.
--   - moderation_audit_log.target_uid preserva historico pos-exclusao.
-- =============================================================

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_device_push_tokens_user') THEN
        ALTER TABLE device_push_tokens
            ADD CONSTRAINT fk_device_push_tokens_user
            FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vital_sign_changes_vital') THEN
        ALTER TABLE vital_sign_changes
            ADD CONSTRAINT fk_vital_sign_changes_vital
            FOREIGN KEY (vital_id) REFERENCES vital_signs(id) ON DELETE CASCADE NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_notification_sources_patient') THEN
        ALTER TABLE notification_sources
            ADD CONSTRAINT fk_notification_sources_patient
            FOREIGN KEY (patient_id) REFERENCES profiles(id) ON DELETE CASCADE NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_feedbacks_user') THEN
        ALTER TABLE feedbacks
            ADD CONSTRAINT fk_feedbacks_user
            FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_feedback_replies_user') THEN
        ALTER TABLE feedback_replies
            ADD CONSTRAINT fk_feedback_replies_user
            FOREIGN KEY (user_id) REFERENCES profiles(id) ON DELETE CASCADE NOT VALID;
    END IF;
END $$;
