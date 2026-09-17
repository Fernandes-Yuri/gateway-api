-- =============================================================
-- MIGRACAO 008: Ecossistema de Exames, Insercao Manual e Consentimento LGPD
-- Suporte aos contratos formais de software (08_CADERNO_DE_CONTRATOS_EXAMES_E_DOSSIE.md)
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Garante a criacao ou evolucao da tabela de exames
CREATE TABLE IF NOT EXISTS exams (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    exam_date DATE NOT NULL,
    file_url TEXT,
    exam_type VARCHAR(50) NOT NULL DEFAULT 'LAB',
    is_cloud_stored BOOLEAN NOT NULL DEFAULT FALSE,
    cloud_consent_accepted BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(50) DEFAULT 'confirmed',
    validation_notes TEXT,
    ai_extracted_data TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Permite que file_url seja nulo para exames digitados manualmente
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'exams' AND column_name = 'file_url' AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE exams ALTER COLUMN file_url DROP NOT NULL;
    END IF;
END $$;

-- Garante colunas de classificacao e consentimento na tabela exams
ALTER TABLE exams ADD COLUMN IF NOT EXISTS exam_type VARCHAR(50) NOT NULL DEFAULT 'LAB';
ALTER TABLE exams ADD COLUMN IF NOT EXISTS is_cloud_stored BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE exams ADD COLUMN IF NOT EXISTS cloud_consent_accepted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_exams_user ON exams(user_id, exam_date DESC);
CREATE INDEX IF NOT EXISTS idx_exams_type ON exams(exam_type);

-- 2. Garante a criacao da tabela de itens de exames
CREATE TABLE IF NOT EXISTS exam_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    exam_id UUID NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    item_key VARCHAR(100) NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    value_numeric DOUBLE PRECISION NOT NULL,
    value_text TEXT NOT NULL,
    unit VARCHAR(50) NOT NULL,
    reference_text TEXT,
    status VARCHAR(50) DEFAULT 'confirmed',
    measured_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exam_items_user_key ON exam_items(user_id, item_key);
CREATE INDEX IF NOT EXISTS idx_exam_items_exam ON exam_items(exam_id);

-- 3. Tabela de Auditoria e Consentimento LGPD com Isencao de Ataques (Art. 43, III)
CREATE TABLE IF NOT EXISTS exam_cloud_consents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    exam_id UUID NOT NULL,
    terms_version VARCHAR(20) NOT NULL,
    accepted_cloud_storage BOOLEAN NOT NULL DEFAULT TRUE,
    accepted_third_party_attack_waiver BOOLEAN NOT NULL DEFAULT TRUE,
    device_fingerprint VARCHAR(150),
    ip_address VARCHAR(45) NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exam_consents_user ON exam_cloud_consents(user_id);
CREATE INDEX IF NOT EXISTS idx_exam_consents_exam ON exam_cloud_consents(exam_id);

INSERT INTO schema_migrations(version) VALUES ('008_exams_and_cloud_consents') ON CONFLICT DO NOTHING;
