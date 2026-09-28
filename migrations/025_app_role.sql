-- =============================================================
-- MIGRACAO 025: Separacao de privilegios - role bragasaude_app
-- Data: 29/09/2026
-- T-06 / SEG-2: menor privilegio para o app
--
-- Esta migration cria o role bragasaude_app com acesso DML apenas.
-- O superuser (bragasaude_user) continua sendo usado APENAS para
-- executar migrations futuras. O app passa a conectar como
-- bragasaude_app apos atualizacao do DATABASE_URL.
--
-- IMPORTANTE: A senha abaixo eh um placeholder. O fundador DEVE
-- gerar uma senha forte e substituir ANTES de aplicar em producao.
-- Comando: ALTER ROLE bragasaude_app WITH PASSWORD '<SENHA_FORTE>';
-- =============================================================

-- Cria role com login (senha placeholder - trocar antes de aplicar)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bragasaude_app') THEN
        CREATE ROLE bragasaude_app WITH LOGIN PASSWORD 'TROCAR_ANTES_DE_APLICAR';
    END IF;
END
$$;

-- Acesso ao banco e schema
GRANT CONNECT ON DATABASE braga_saude TO bragasaude_app;
GRANT USAGE ON SCHEMA public TO bragasaude_app;

-- DML: SELECT, INSERT, UPDATE, DELETE em todas as tabelas existentes
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bragasaude_app;

-- Sequencias (para colunas SERIAL/IDENTITY)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bragasaude_app;

-- Garante que tabelas futuras tambem herdem os privilegios
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bragasaude_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO bragasaude_app;

-- Revoga explicitamente qualquer capacidade DDL
-- (CREATE, ALTER, DROP, TRUNCATE, REFERENCES, TRIGGER)
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM bragasaude_app;
