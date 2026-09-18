-- 006_whatsapp_totp_linking.sql
-- Vinculo de WhatsApp por TOTP (arquitetura temporaria, ate a Meta liberar template).
-- Cada conta nasce com um segredo TOTP sob o capo; o webhook recomputa o codigo e
-- vincula o numero verificado pela Meta a conta. Janela de 180s, sem tolerancia.

-- Segredo TOTP da conta (base32, 20 bytes). Nullable: contas pre-existentes
-- recebem na proxima sincronizacao de perfil.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(64);

-- Numero de WhatsApp vinculado (normalizado, so digitos, com DDI).
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS whatsapp_phone VARCHAR(32);
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS whatsapp_linked_at TIMESTAMPTZ;

-- Tentativas de vinculo: auditoria (LGPD), cap de retentativas e sinal de
-- friccao no onboarding.
CREATE TABLE IF NOT EXISTS whatsapp_link_attempts (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sender_phone  VARCHAR(32) NOT NULL,
    code_received VARCHAR(10)  NOT NULL,
    matched_user  VARCHAR(128),
    status        VARCHAR(20)  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_link_attempts_phone
    ON whatsapp_link_attempts (sender_phone, created_at DESC);

-- Limpa tentativas antigas: so importa o cap em janela recente.
CREATE INDEX IF NOT EXISTS idx_wa_link_attempts_created
    ON whatsapp_link_attempts (created_at);
