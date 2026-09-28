-- =============================================================
-- ROLLBACK 025: Remove role bragasaude_app
-- Data: 29/09/2026
-- Executar se algo der errado - reverte para superuser unico
-- =============================================================

-- Revoga todos os privilegios antes de remover o role
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM bragasaude_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM bragasaude_app;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM bragasaude_app;
REVOKE USAGE ON SCHEMA public FROM bragasaude_app;
REVOKE CONNECT ON DATABASE braga_saude FROM bragasaude_app;

-- Remove default privileges futuros
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM bragasaude_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM bragasaude_app;

-- Remove o role
DROP ROLE IF EXISTS bragasaude_app;
