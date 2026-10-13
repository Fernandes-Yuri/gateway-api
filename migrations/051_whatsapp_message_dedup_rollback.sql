-- =============================================================
-- ROLLBACK 051: remove a antiduplicidade de mensagens do WhatsApp.
-- VOLTA O ESTADO: reenvios da Meta voltam a reprocessar mensagens
-- (estado ANTES desta migration). Use so se for reverter o PR1.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

DROP TABLE IF EXISTS whatsapp_operation_keys;
DROP TABLE IF EXISTS whatsapp_message_receipts;

DELETE FROM schema_migrations WHERE version = '051_whatsapp_message_dedup';
COMMIT;
