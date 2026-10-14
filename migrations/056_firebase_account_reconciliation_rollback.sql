BEGIN;
DROP TABLE IF EXISTS account_auth_observations;
-- Manter jobs pendentes: desativar scheduler antes de executar rollback.
ALTER TABLE account_storage_cleanup DROP COLUMN IF EXISTS firebase_cleanup_required;
DELETE FROM schema_migrations WHERE version='056_firebase_account_reconciliation';
COMMIT;
