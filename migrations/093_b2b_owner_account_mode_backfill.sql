-- =============================================================
-- MIGRACAO 093 (B2B-30): backfill de account_mode dos responsaveis
-- de programa ativo.
--
-- Contexto: a 092b criou profiles.account_mode com DEFAULT 'PERSONAL'
-- e nao migrou os responsaveis de programa ja existentes. Como
-- GET /api/session/contexts (alias /api/b2b/contexts) filtra o
-- contexto PARTNER_OWNER quando account_mode = 'PERSONAL', owners
-- antigos sumiriam dos dois apps (antigo e novo) depois do deploy.
--
-- Este backfill idempotente promove para 'DUAL' (e nao BUSINESS_ONLY)
-- apenas contas 'PERSONAL' que sao responsaveis de programa nao
-- encerrado, preservando o espaco pessoal/participante que a conta
-- ja tinha. Contas 'BUSINESS_ONLY' e 'DUAL' existentes nao sao tocadas.
--
-- Aditivo e idempotente: rodar novamente nao causa efeito extra.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

UPDATE profiles
   SET account_mode = 'DUAL',
       updated_at = NOW()
 WHERE account_mode = 'PERSONAL'
   AND id IN (
       SELECT owner_user_id
         FROM partner_programs
        WHERE closed_at IS NULL
   );
