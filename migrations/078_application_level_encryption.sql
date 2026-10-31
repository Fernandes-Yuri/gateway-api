-- =============================================================
-- MIGRACAO 078: Criptografia a Nivel de Aplicacao e Blind Indexing (SPEC-12)
-- Protege PII e dados clinicos em repouso no RDS com AES-256-GCM
-- (`enc:v1:<iv_b64>:<ciphertext_com_tag_b64>`) e buscas O(1) via
-- Blind Indexing HMAC-SHA256 (`phone_bindex`).
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

-- 1. Tabela dedicada de contatos de emergencia com cifragem autenticada e blind index (SPEC-12 §3.1)
CREATE TABLE IF NOT EXISTS emergency_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT,
    relation VARCHAR(100),
    phone TEXT,
    phone_encrypted TEXT,
    phone_bindex VARCHAR(64),
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE emergency_contacts ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE emergency_contacts ADD COLUMN IF NOT EXISTS phone_encrypted TEXT;
ALTER TABLE emergency_contacts ADD COLUMN IF NOT EXISTS phone_bindex VARCHAR(64);
ALTER TABLE emergency_contacts ALTER COLUMN phone TYPE TEXT;
ALTER TABLE emergency_contacts ALTER COLUMN phone_encrypted TYPE TEXT;

CREATE INDEX IF NOT EXISTS idx_emergency_contacts_user_id
    ON emergency_contacts (user_id);
CREATE INDEX IF NOT EXISTS idx_emergency_contacts_phone_bindex
    ON emergency_contacts (phone_bindex);

-- 2. Ajuste de tipos e blind index em profiles para suportar payloads enc:v1: (SPEC-12 §3.2)
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE profiles ALTER COLUMN phone TYPE TEXT;
ALTER TABLE profiles ALTER COLUMN emergency_contact_phone TYPE TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS phone_bindex VARCHAR(64);
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS emergency_contact_phone_bindex VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_profiles_phone_bindex
    ON profiles (phone_bindex)
    WHERE phone_bindex IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_profiles_emergency_contact_phone_bindex
    ON profiles (emergency_contact_phone_bindex)
    WHERE emergency_contact_phone_bindex IS NOT NULL;

-- 3. Ajuste de tipos em feedback_replies e notas clinicas (symptoms_diary) para payloads AES-256-GCM (SPEC-12 §3.3)
ALTER TABLE feedback_replies ALTER COLUMN body TYPE TEXT;
ALTER TABLE symptoms_diary ALTER COLUMN symptoms_text TYPE TEXT;

INSERT INTO schema_migrations (version)
VALUES ('078_application_level_encryption')
ON CONFLICT (version) DO NOTHING;

COMMIT;
