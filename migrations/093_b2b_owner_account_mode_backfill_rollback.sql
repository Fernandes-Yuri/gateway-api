-- =============================================================
-- Rollback da migracao 093 (backfill de account_mode dos owners).
--
-- ATENCAO: rollback best-effort. A 093 nao registra o valor anterior,
-- portanto nao ha inversa exata: contas que ja eram 'DUAL' por escolha
-- explicita do usuario tambem seriam rebaixadas para 'PERSONAL'.
-- Use apenas em emergencia, com o app antigo/novo pausado, e reaplique
-- a 093 depois se o rollback do backfill nao for intencional.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

UPDATE profiles
   SET account_mode = 'PERSONAL',
       updated_at = NOW()
 WHERE account_mode = 'DUAL'
   AND id IN (
       SELECT owner_user_id
         FROM partner_programs
        WHERE closed_at IS NULL
   );
