ALTER TABLE device_push_tokens DROP CONSTRAINT IF EXISTS fk_device_push_tokens_user;
ALTER TABLE vital_sign_changes DROP CONSTRAINT IF EXISTS fk_vital_sign_changes_vital;
ALTER TABLE notification_sources DROP CONSTRAINT IF EXISTS fk_notification_sources_patient;
ALTER TABLE feedbacks DROP CONSTRAINT IF EXISTS fk_feedbacks_user;
ALTER TABLE feedback_replies DROP CONSTRAINT IF EXISTS fk_feedback_replies_user;
