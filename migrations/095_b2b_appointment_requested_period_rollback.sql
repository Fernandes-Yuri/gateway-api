-- =============================================================
-- Rollback da migracao 095 (periodo desejado do agendamento).
--
-- A coluna nasceu NULL e nunca teve backfill: nao ha dado anterior a
-- restaurar. O rollback remove o invariante e a coluna.
--
-- IMPORTANTE: reverta TAMBEM o binario do braga-edge neste rollback. Os
-- SELECTs de agendamento passaram a listar `requested_period`; com a coluna
-- removida e o binario novo no ar a consulta falha (500). Ordem segura:
-- parar o servico -> rollback -> deploy do binario anterior -> subir.
--
-- Idempotente: DROP ... IF EXISTS em ambos os passos.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE program_appointments
    DROP CONSTRAINT IF EXISTS chk_program_appointments_requested_period;

ALTER TABLE program_appointments
    DROP COLUMN IF EXISTS requested_period;
