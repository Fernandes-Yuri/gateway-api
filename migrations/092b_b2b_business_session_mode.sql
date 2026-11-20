-- =============================================================
-- MIGRACAO 092b (B2B-30): Sessao Empresarial Pura (account_mode)
--
-- Adiciona a coluna aditiva profiles.account_mode separando a
-- sessao de negocio (BUSINESS_ONLY / DUAL) da sessao de paciente
-- (PERSONAL), mantendo o mesmo login/autenticacao.
--
-- Retrocompatibilidade com v1.9.6-build43:
--   - Nao altera user_role ('PATIENT'/'CAREGIVER'), nem a
--     constraint profiles_completion_check: contas BUSINESS_ONLY
--     permanecem com user_role = NULL, basic_profile_complete = FALSE
--     e self_care_complete = FALSE (dispensadas do onboarding de
--     peso/altura), sem violar nenhuma regra existente.
--   - Coluna nova com DEFAULT 'PERSONAL': todo usuario atual segue
--     exatamente no fluxo B2C de sempre.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS account_mode varchar(24) NOT NULL DEFAULT 'PERSONAL';

ALTER TABLE profiles
    DROP CONSTRAINT IF EXISTS profiles_account_mode_check;

ALTER TABLE profiles
    ADD CONSTRAINT profiles_account_mode_check
    CHECK (account_mode IN ('PERSONAL', 'BUSINESS_ONLY', 'DUAL'));
