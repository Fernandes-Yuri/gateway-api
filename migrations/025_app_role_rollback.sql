-- 025_app_role_rollback.sql
-- ============================================================
-- ROLLBACK: Remove o role bragasaude_app.
--
-- Execute APENAS se precisar reverter a separacao de privilegios.
-- CUIDADO: isso afetara qualquer aplicacao que use bragasaude_app.
-- ============================================================

BEGIN;

-- Remove default privileges primeiro
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM bragasaude_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM bragasaude_app;

-- Revoga todos os privilegios concedidos
REVOKE ALL PRIVILEGES ON DATABASE bragasaude FROM bragasaude_app;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM bragasaude_app;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM bragasaude_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM bragasaude_app;

-- Remove o role
DROP ROLE IF EXISTS bragasaude_app;

-- Remove registro da migration (marca como revertida)
DELETE FROM schema_migrations WHERE version = '025_app_role';

COMMIT;