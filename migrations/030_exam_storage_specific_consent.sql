-- Preserva recibos anteriores e registra as duas declarações do novo termo.
ALTER TABLE exam_cloud_consents ALTER COLUMN terms_version TYPE VARCHAR(64);
ALTER TABLE exam_cloud_consents ALTER COLUMN accepted_third_party_attack_waiver SET DEFAULT FALSE;
ALTER TABLE exam_cloud_consents ADD COLUMN IF NOT EXISTS valid_exam_attested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE exam_cloud_consents ADD COLUMN IF NOT EXISTS non_diagnostic_purpose_accepted BOOLEAN NOT NULL DEFAULT FALSE;

INSERT INTO schema_migrations(version) VALUES ('030_exam_storage_specific_consent') ON CONFLICT DO NOTHING;
