-- =============================================================
-- MIGRACAO 088: Expurgo de tabelas zumbis/mortas
--
-- 1. biometries: tabela precursora criada no init_schema.sql e abandonada
--    desde a criacao da tabela canonical biometrics (migracao 053).
-- 2. confirmation_logs: tabela do antigo modulo de saude WhatsApp (migracao 004),
--    cujo modulo foi desativado nas migracoes 066 e 076.
-- 3. pending_confirmations: tabela do antigo modulo WhatsApp (migracoes 004/057),
--    igualmente desativada nas migracoes 066 e 076.
-- =============================================================

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';

DROP TABLE IF EXISTS public.confirmation_logs CASCADE;
DROP TABLE IF EXISTS public.pending_confirmations CASCADE;
DROP TABLE IF EXISTS public.biometries CASCADE;
