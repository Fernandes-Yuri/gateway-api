-- =============================================================
-- MIGRACAO 015: Colunas de 2FA (TOTP) em admin_users
--
-- O webservice (portal) autentica admins nesta tabela. As colunas
-- de 2FA por conta (D-WS4) existiam apenas no initDatabase() do
-- Next.js — que NAO consegue mais chegar no RDS (ele roda no Render,
-- fora da VPC; o RDS e privado). O gateway, na EC2, e quem tem
-- conectividade. Esta migration torna o schema oficial e remove a
-- dependencia do initDatabase() para criar colunas.
--
-- Idempotente (IF NOT EXISTS).
-- =============================================================

ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_secret         VARCHAR(255);
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_enabled        BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_recovery_codes TEXT[]      NOT NULL DEFAULT '{}';

INSERT INTO schema_migrations(version) VALUES ('015_admin_users_totp') ON CONFLICT DO NOTHING;
