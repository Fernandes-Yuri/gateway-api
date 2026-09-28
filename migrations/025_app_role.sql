-- 025_app_role.sql
-- ============================================================
-- Cria role aplikasi bragasaude_app com privilegios limitados.
--
-- Purpose:
--   - Separar privilegios entre superuser (gateway-api) e aplicacao.
--   - O role app NAO pode executar DDL (CREATE/DROP/ALTER TABLE, etc).
--
-- Apply: python migrate.py
-- Rollback: 025_app_role_rollback.sql
-- ============================================================

-- Role untuk aplikasi dengan password placeholder (ganti sebelum production!)
CREATE ROLE bragasaude_app WITH LOGIN PASSWORD 'PgApp2024@braga#xK9$mNq';

-- Privilégios de conexao no banco
GRANT CONNECT ON DATABASE bragasaude TO bragasaude_app;

-- Privilégios de uso no schema public
GRANT USAGE ON SCHEMA public TO bragasaude_app;

-- Privilégios DML em todas as tabelas existentes
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bragasaude_app;

-- Privilégios em todas as sequencias (para auto-increment / serial)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bragasaude_app;

-- ============================================================
-- REVOKE todo DDL — o role app NAO pode alterar o schema.
-- ============================================================

-- Nao pode criar objetos no schema public
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;

-- Nao pode criar tabela (REVOKE CREATE ja bloqueia, mas e bom ser explicito)
REVOKE CREATE ON DATABASE bragasaude FROM bragasaude_app;

-- Nao pode dropar objetos existentes
REVOKE DROP ON ALL TABLES IN SCHEMA public FROM bragasaude_app;

-- Nao pode alterar objetos existentes
REVOKE ALTER ON ALL TABLES IN SCHEMA public FROM bragasaude_app;

-- Nao pode criar foreign keys (REFERENCES)
REVOKE REFERENCES ON ALL TABLES IN SCHEMA public FROM bragasaude_app;

-- Nao pode criar triggers
REVOKE TRIGGER ON ALL TABLES IN SCHEMA public FROM bragasaude_app;

-- Nao pode criar indices
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;

-- Nao pode criar sequencias (ja tem USAGE, mas nao CREATE)
-- O USAGE + SELECT permite usar serial/sequence, mas CREATE permitiria criar novas
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;

-- Nao pode criar types ou domínios
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;

-- Nao pode criar functions ou procedures
REVOKE CREATE ON SCHEMA public FROM bragasaude_app;

-- ============================================================
-- DEFAULT PRIVILEGES: garante que futuras tabelas tambem respeitem
-- ============================================================
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bragasaude_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO bragasaude_app;

-- Nao permite DDL em novas tabelas
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE CREATE ON TABLES FROM bragasaude_app;

-- Registra migration (idempotente)
INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('025_app_role', 'app_role_limited_privileges', now())
ON CONFLICT (version) DO NOTHING;