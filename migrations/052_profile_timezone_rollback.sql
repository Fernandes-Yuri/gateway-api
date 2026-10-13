-- =============================================================
-- ROLLBACK 052: remove a coluna de fuso do perfil.
-- VOLTA O ESTADO: o servico volta a usar America/Sao_Paulo fixo.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE profiles DROP COLUMN IF EXISTS timezone;

DELETE FROM schema_migrations WHERE version = '052_profile_timezone';
COMMIT;
