-- =============================================================
-- ROLLBACK 050: remove 'aguardando_segundo_aceite' do CHECK de status.
-- VOLTA O ESTADO: acoes totp_duplo voltam a quebrar na transicao
-- (estado ANTES desta migration). Use so se for reverter o DT-009.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE sentinel_actions DROP CONSTRAINT IF EXISTS sentinel_actions_status_check;

ALTER TABLE sentinel_actions ADD CONSTRAINT sentinel_actions_status_check
  CHECK (status IN ('proposta','aguardando_totp','aprovada','negada','expirada','executada','falhou'));

DELETE FROM schema_migrations WHERE version = '050_sentinel_status_segundo_aceite';
COMMIT;
