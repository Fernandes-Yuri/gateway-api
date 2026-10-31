-- =============================================================
-- MIGRACAO 079: Criptografia Ponta a Ponta (E2EE) no Chat Familiar (SPEC-13)
-- Suporte a troca de chaves publicas ECDH nos vinculos familiares e
-- persistencia Blind Relay de blobs cifrados AES-256-GCM em family_messages.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

-- 1. Chaves publicas assimetricas para acordo ECDH no vinculo familiar (SPEC-13 §2.1)
ALTER TABLE family_bindings ADD COLUMN IF NOT EXISTS patient_public_key TEXT;
ALTER TABLE family_bindings ADD COLUMN IF NOT EXISTS caregiver_public_key TEXT;

-- 2. Colunas de Blind Relay e versionamento E2EE em family_messages (SPEC-13 §2.3 e §3)
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS body_encrypted TEXT;
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS encryption_version SMALLINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN family_bindings.patient_public_key IS 'SPEC-13: chave publica ECDH (SubjectPublicKeyInfo Base64) do dispositivo do paciente';
COMMENT ON COLUMN family_bindings.caregiver_public_key IS 'SPEC-13: chave publica ECDH (SubjectPublicKeyInfo Base64) do dispositivo do cuidador';
COMMENT ON COLUMN family_messages.body_encrypted IS 'SPEC-13: blob cifrado ponta a ponta AES-256-GCM (JSON v1 iv+ciphertext), indecifravel pelo servidor';
COMMENT ON COLUMN family_messages.encryption_version IS 'SPEC-13: 0 = legado texto plano, 1 = E2EE AES-256-GCM';

INSERT INTO schema_migrations (version)
VALUES ('079_family_messages_e2ee')
ON CONFLICT (version) DO NOTHING;

COMMIT;
