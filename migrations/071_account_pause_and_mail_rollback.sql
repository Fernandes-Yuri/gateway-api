BEGIN;
-- Reativar explicitamente as contas antes de remover a pausa e sua fila de avisos.
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM profiles WHERE paused_at IS NOT NULL) THEN
        RAISE EXCEPTION 'Existem contas pausadas; reative-as antes do rollback';
    END IF;
    IF EXISTS(SELECT 1 FROM account_lifecycle_mail) THEN
        RAISE EXCEPTION 'Existem comprovantes pendentes; processe a fila antes do rollback';
    END IF;
END $$;
DROP TABLE IF EXISTS account_lifecycle_mail;
ALTER TABLE profiles DROP COLUMN IF EXISTS paused_at;
ALTER TABLE user_deletion_logs DROP COLUMN IF EXISTS confirmation_sent_at;
DELETE FROM schema_migrations WHERE version='071_account_pause_and_mail';
COMMIT;
