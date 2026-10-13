-- Formaliza coluna usada pela API de sincronização e pelas consultas WhatsApp.
-- Compatível tanto com bancos legados que já têm a coluna quanto com o baseline.
BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE vital_signs ADD COLUMN IF NOT EXISTS oxygen_saturation INTEGER;
INSERT INTO schema_migrations(version) VALUES ('054_vital_signs_oxygen') ON CONFLICT DO NOTHING;
COMMIT;
