-- =============================================================
-- MIGRACAO 051: antiduplicidade de mensagens e chaves de operacao do
-- WhatsApp (PR1, passo 1.4).
--
-- A Meta reenvia uma mensagem enquanto nao recebe ack (ou em situacoes de
-- rede). Sem persistencia, o reenvio reprocessava a mesma mensagem: nova
-- pendencia de confirmacao, novo registro de saude, dose dobrada. Memoria do
-- processo nao serve: o deploy roda em mais de um processo e um restart
-- perde tudo.
--
-- Agora:
--   whatsapp_message_receipts: cada mensagem Meta processada e registrada por
--     message_id (PK). Reenvio = mesma mensagem = nao reprocessa.
--   whatsapp_operation_keys: chave de operacao derivada do message_id + tipo
--     (ex.: "take:CONF-..."), com unicidade no banco. O reenvio de uma
--     operacao de gravacao encontra a chave e nao duplica. Deve ser reservada
--     na mesma transacao da gravacao efetiva.
--
-- Checado antes de criar: nao existem tabelas de dedup de mensagens; profiles
-- e a base de uuids ja existem (009/017). Reversivel: DROP das duas tabelas.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS whatsapp_message_receipts (
    message_id   VARCHAR(128) PRIMARY KEY,
    user_id      VARCHAR(128) REFERENCES profiles(id) ON DELETE CASCADE,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatsapp_operation_keys (
    id            BIGSERIAL    PRIMARY KEY,
    message_id    VARCHAR(128) NOT NULL,
    operation_key VARCHAR(200) NOT NULL,
    actor_id      VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE whatsapp_operation_keys
    ADD CONSTRAINT whatsapp_operation_keys_operation_key_key UNIQUE (operation_key);

CREATE INDEX IF NOT EXISTS idx_wa_op_keys_message
    ON whatsapp_operation_keys (message_id);

INSERT INTO schema_migrations(version) VALUES ('051_whatsapp_message_dedup') ON CONFLICT DO NOTHING;
COMMIT;
