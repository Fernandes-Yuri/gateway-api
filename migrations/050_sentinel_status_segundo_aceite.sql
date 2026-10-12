-- =============================================================
-- MIGRACAO 050: adiciona 'aguardando_segundo_aceite' ao CHECK de
-- status de sentinel_actions.
--
-- O DT-009 (repo sentinela-bot) introduziu a maquina de estados do
-- segundo fator: acoes 'totp_duplo' (ex.: stop_instance) param em
-- 'aguardando_segundo_aceite' entre o TOTP valido e o segundo aceite.
-- A migration 039, que espelha as tabelas do sentinela, preveia esse
-- status? Nao — o CHECK so liberava proposta/aguardando_totp/aprovada/
-- negada/expirada/executada/falhou.Resultado: qualquer acao
-- totp_duplo na vida real quebrava na transicao com
-- CheckViolationError (descoberto em producao, rodando a prova do
-- DT-009 contra o banco real).
--
-- Complemento, nao retrabalho: so adiciona o valor faltante.
-- Reversivel: DROP CONSTRAINT + reconstrucao sem o valor.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE sentinel_actions DROP CONSTRAINT IF EXISTS sentinel_actions_status_check;

ALTER TABLE sentinel_actions ADD CONSTRAINT sentinel_actions_status_check
  CHECK (status IN ('proposta','aguardando_totp','aguardando_segundo_aceite',
                    'aprovada','negada','expirada','executada','falhou'));

INSERT INTO schema_migrations(version) VALUES ('050_sentinel_status_segundo_aceite') ON CONFLICT DO NOTHING;
COMMIT;
