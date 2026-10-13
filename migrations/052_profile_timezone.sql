-- =============================================================
-- MIGRACAO 052: fuso horario cadastrado no perfil (PR2, passo 2.2).
--
-- "Hoje" no WhatsApp e no app precisa ser o dia do paciente, no fuso dele,
-- nunca a data do relogio do servidor (EC2 roda em UTC). Sem fuso
-- cadastrado, o servico usa America/Sao_Paulo.
--
-- Coluna opcional: o app ainda nao envia; o backend ja trata NULL como
-- America/Sao_Paulo. Quando o app enviar (sync de perfil), passa a valer.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS timezone VARCHAR(64);

INSERT INTO schema_migrations(version) VALUES ('052_profile_timezone') ON CONFLICT DO NOTHING;
COMMIT;
