-- =============================================================
-- ROLLBACK 053: remove a tabela de biometria do backend.
-- VOLTA O ESTADO: peso do WhatsApp volta a gravar só em vital_signs
-- (divergência do app, estado ANTES desta migration).
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

DROP TABLE IF EXISTS biometrics;

DELETE FROM schema_migrations WHERE version = '053_biometrics';
COMMIT;
