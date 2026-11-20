-- Rollback da MIGRACAO 092b (B2B-30): remove profiles.account_mode.
-- Nao toca em user_role, constraints ou dados pessoais existentes.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE profiles
    DROP CONSTRAINT IF EXISTS profiles_account_mode_check;

ALTER TABLE profiles
    DROP COLUMN IF EXISTS account_mode;
