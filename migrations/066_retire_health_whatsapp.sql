-- Encerra o assistente WhatsApp de saúde. Não remove contas ou registros do app.
-- Limpeza irreversível dos vínculos e dados exclusivos de teste autorizada em 19/10/2026.
-- O código antigo e seus emissores devem ser retirados antes desta migração.
SET LOCAL lock_timeout = '10s';

DELETE FROM confirmation_logs WHERE channel = 'whatsapp';
-- Logs de outros canais podem apontar a uma proposta antiga: preservar o log.
UPDATE confirmation_logs SET pending_conf_id = NULL
WHERE pending_conf_id IN (SELECT id FROM pending_confirmations WHERE source_channel = 'whatsapp');
DELETE FROM pending_confirmations WHERE source_channel = 'whatsapp';
DELETE FROM agent_actions WHERE channel = 'whatsapp';
-- Propostas de lote são transitórias; tomadas e ocorrências clínicas permanecem.
DELETE FROM medication_dose_batches WHERE channel = 'whatsapp';

-- As tabelas de confirmação são compartilhadas: somente as tabelas exclusivas saem.
-- Recibos dos agendamentos exclusivos do atendimento antigo.
DROP TABLE IF EXISTS care_notification_deliveries;
DROP TABLE IF EXISTS whatsapp_channel_cleanup;
DROP TABLE IF EXISTS whatsapp_channel_deletions;
DROP TABLE IF EXISTS whatsapp_channel_consents;
DROP TABLE IF EXISTS whatsapp_operation_keys;
DROP TABLE IF EXISTS whatsapp_message_receipts;
DROP TABLE IF EXISTS whatsapp_link_attempts;

ALTER TABLE profiles DROP COLUMN IF EXISTS whatsapp_phone;
ALTER TABLE profiles DROP COLUMN IF EXISTS whatsapp_linked_at;
ALTER TABLE profiles DROP COLUMN IF EXISTS totp_secret;
