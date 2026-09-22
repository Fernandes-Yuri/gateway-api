-- =============================================================
-- ESQUEMA POSTGRESQL 16 - BRAGA SAÚDE HOMELAB DOCKER
-- Tradução direta do schema.gql (Firebase Data Connect)
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. PERFIS DE USUÁRIO (Titular, Idoso, Cuidador, Convidado)
CREATE TABLE IF NOT EXISTS profiles (
    id VARCHAR(128) PRIMARY KEY,
    full_name VARCHAR(255) NOT NULL,
    birth_date DATE,
    gender VARCHAR(50),
    height DOUBLE PRECISION,
    weight DOUBLE PRECISION,
    is_smoker BOOLEAN DEFAULT FALSE,
    has_diabetes BOOLEAN DEFAULT FALSE,
    has_hypertension BOOLEAN DEFAULT FALSE,
    has_thyroid_issue BOOLEAN DEFAULT FALSE,
    has_renal_issue BOOLEAN DEFAULT FALSE,
    has_bone_issue BOOLEAN DEFAULT FALSE,
    has_muscular_issue BOOLEAN DEFAULT FALSE,
    hydration_target_ml INT DEFAULT 2000,
    daily_calorie_target INT DEFAULT 1800,
    step_goal INT DEFAULT 8000,
    weight_goal DOUBLE PRECISION,
    sleep_start_time VARCHAR(10) DEFAULT '22:00',
    sleep_end_time VARCHAR(10) DEFAULT '06:00',
    emergency_contact_name VARCHAR(255),
    emergency_contact_relation VARCHAR(100),
    emergency_contact_phone VARCHAR(50),
    consent_accepted_at TIMESTAMPTZ,
    notifications_enabled BOOLEAN DEFAULT TRUE,
    location_enabled BOOLEAN DEFAULT TRUE,
    current_xp INT DEFAULT 0,
    total_xp INT DEFAULT 0,
    current_level INT DEFAULT 1,
    current_streak INT DEFAULT 0,
    last_xp_at TIMESTAMPTZ,
    show_in_feed BOOLEAN DEFAULT TRUE,
    user_role VARCHAR(50) DEFAULT 'PATIENT',
    caregiver_mode VARCHAR(50),
    avatar_identifier VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. SINAIS VITAIS (Pressão, Glicose, Frequência Cardíaca, Hidratação)
CREATE TABLE IF NOT EXISTS vital_signs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    systolic_pressure INT,
    diastolic_pressure INT,
    heart_rate INT,
    glucose_level INT,
    glucose_type VARCHAR(50),
    hydration_ml INT,
    measured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vitals_user_measured ON vital_signs(user_id, measured_at DESC);

-- 3. MEDICAMENTOS E HORÁRIOS
CREATE TABLE IF NOT EXISTS medications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    dosage_mg DOUBLE PRECISION,
    pill_quantity INT,
    schedule_time VARCHAR(10) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_medications_user ON medications(user_id);

-- 4. REGISTRO DE MEDICAMENTOS TOMADOS
CREATE TABLE IF NOT EXISTS medication_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    medication_id UUID NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    taken_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_medication_logs_user_taken ON medication_logs(user_id, taken_at DESC);

-- 5. EXAMES MÉDICOS EM PDF
CREATE TABLE IF NOT EXISTS exams (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    exam_date DATE NOT NULL,
    file_url TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'analyzed',
    validation_notes TEXT,
    ai_extracted_data TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exams_user ON exams(user_id, exam_date DESC);

-- 6. ITENS EXTRAÍDOS DE EXAMES
CREATE TABLE IF NOT EXISTS exam_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    exam_id UUID NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    item_key VARCHAR(100) NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    value_numeric DOUBLE PRECISION,
    value_text TEXT,
    unit VARCHAR(50),
    reference_text TEXT,
    status VARCHAR(50) DEFAULT 'uploaded',
    measured_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exam_items_user_key ON exam_items(user_id, item_key);

-- 7. REFERÊNCIAS CLÍNICAS OFICIAIS (SBC, SBD, OMS)
CREATE TABLE IF NOT EXISTS clinical_references (
    item_key VARCHAR(100) PRIMARY KEY,
    item_name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    unit VARCHAR(50),
    min_target DOUBLE PRECISION,
    max_target DOUBLE PRECISION,
    min_critical DOUBLE PRECISION,
    max_critical DOUBLE PRECISION,
    interpretation_hint TEXT,
    institution VARCHAR(255),
    document_version VARCHAR(255),
    parameter VARCHAR(255),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8. BIOMETRIA (Peso, Altura, IMC)
CREATE TABLE IF NOT EXISTS biometries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    weight DOUBLE PRECISION NOT NULL,
    height DOUBLE PRECISION NOT NULL,
    imc DOUBLE PRECISION NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_biometries_user_measured ON biometries(user_id, measured_at DESC);

-- 9. MÉTRICAS DIÁRIAS DE ATIVIDADE FÍSICA (Passos, Distância, Calorias)
CREATE TABLE IF NOT EXISTS daily_metrics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    steps INT NOT NULL DEFAULT 0,
    distance_meters DOUBLE PRECISION NOT NULL DEFAULT 0,
    distance_gps_meters DOUBLE PRECISION,
    distance_steps_meters DOUBLE PRECISION,
    distance_final_meters DOUBLE PRECISION,
    reliability_score DOUBLE PRECISION,
    calories_burned DOUBLE PRECISION NOT NULL DEFAULT 0,
    active_minutes INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_user_daily_metric UNIQUE (user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_metrics_user_date ON daily_metrics(user_id, date DESC);

-- 10. CATÁLOGO DE ALIMENTOS
CREATE TABLE IF NOT EXISTS food_catalog (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    kcal DOUBLE PRECISION NOT NULL,
    carbs DOUBLE PRECISION NOT NULL,
    proteins DOUBLE PRECISION NOT NULL,
    fats DOUBLE PRECISION NOT NULL,
    sodium DOUBLE PRECISION NOT NULL,
    is_diabetes_safe BOOLEAN DEFAULT TRUE,
    is_hypertension_safe BOOLEAN DEFAULT TRUE,
    is_thyroid_safe BOOLEAN DEFAULT TRUE
);

-- 11. CONSUMO ALIMENTAR
CREATE TABLE IF NOT EXISTS user_consumptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    food_id INT NOT NULL REFERENCES food_catalog(id),
    consumed_at TIMESTAMPTZ NOT NULL,
    meal_type VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 12. MARCOS E CONQUISTAS (Milestones)
CREATE TABLE IF NOT EXISTS milestones (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    badge_type VARCHAR(100) NOT NULL,
    achieved_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 13. FEEDBACK DO USUÁRIO
CREATE TABLE IF NOT EXISTS feedbacks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128),
    user_email VARCHAR(255),
    user_name VARCHAR(255),
    category VARCHAR(50) NOT NULL,
    title VARCHAR(255),
    message TEXT NOT NULL,
    input_method VARCHAR(50),
    app_version VARCHAR(50),
    device_info TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 14. LIGA SAUDÁVEL (Ciclos Semanais de Gamificação)
CREATE TABLE IF NOT EXISTS league_cycles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    level INT NOT NULL,
    week_start_date DATE NOT NULL,
    week_end_date DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_league_cycles_level_status ON league_cycles(level, status);

-- 15. PARTICIPAÇÃO NA LIGA (League Memberships)
CREATE TABLE IF NOT EXISTS league_memberships (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    league_cycle_id UUID NOT NULL REFERENCES league_cycles(id) ON DELETE CASCADE,
    xp_earned INT DEFAULT 0,
    rank_at_close INT,
    outcome VARCHAR(20),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_user_league_cycle UNIQUE (user_id, league_cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_league_memberships_cycle_xp ON league_memberships(league_cycle_id, xp_earned DESC);

-- 16. FEED SOCIAL GLOBAL
CREATE TABLE IF NOT EXISTS social_posts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    post_type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    related_milestone_id UUID REFERENCES milestones(id) ON DELETE SET NULL,
    is_visible BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_posts_created ON social_posts(created_at DESC);

-- 17. REAÇÕES NO FEED SOCIAL (Apoio / Curtidas)
CREATE TABLE IF NOT EXISTS post_reactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    post_id UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reaction_type VARCHAR(50) DEFAULT 'apoio',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_post_user_reaction UNIQUE (post_id, user_id)
);

-- 18. PONTE FAMILIAR & VÍNCULO CUIDADOR
CREATE TABLE IF NOT EXISTS family_bindings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    caregiver_id VARCHAR(128) REFERENCES profiles(id) ON DELETE SET NULL,
    caregiver_name VARCHAR(255) NOT NULL,
    caregiver_relation VARCHAR(100) NOT NULL,
    connection_code VARCHAR(16) UNIQUE NOT NULL,
    status VARCHAR(20) DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_family_bindings_code ON family_bindings(connection_code);
CREATE INDEX IF NOT EXISTS idx_family_bindings_patient ON family_bindings(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_family_bindings_caregiver ON family_bindings(caregiver_id, status);

-- 19. MENSAGENS DE CARINHO FAMILIAR (D47: retenção máxima de 24h)
CREATE TABLE IF NOT EXISTS family_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    sender_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    sender_name VARCHAR(255) NOT NULL,
    message_text TEXT NOT NULL,
    icon_type VARCHAR(50) DEFAULT 'LOVE',
    is_read BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_family_messages_patient ON family_messages(patient_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_family_messages_expiry ON family_messages(expires_at);

-- D47 (16/09/2026): idempotente para bancos já existentes — TTL de 24h e tombstone de exclusão
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
UPDATE family_messages SET expires_at = sent_at + INTERVAL '24 hours' WHERE expires_at IS NULL;
ALTER TABLE family_messages ALTER COLUMN expires_at SET NOT NULL;
ALTER TABLE family_messages ALTER COLUMN expires_at SET DEFAULT (NOW() + INTERVAL '24 hours');

-- 20. AUDITORIA LGPD (Exclusão de Contas)
CREATE TABLE IF NOT EXISTS user_deletion_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address VARCHAR(50),
    tables_affected TEXT NOT NULL,
    storage_files_cleaned INT DEFAULT 0
);

-- 21. CÓDIGOS DE SEGURANÇA E RECUPERAÇÃO DE CONTA (OTP)
CREATE TABLE IF NOT EXISTS auth_verification_codes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    identifier VARCHAR(255) NOT NULL, -- e-mail ou telefone
    code VARCHAR(10) NOT NULL,        -- código de 6 dígitos
    purpose VARCHAR(50) NOT NULL,     -- 'EMAIL_VERIFICATION' ou 'PASSWORD_RESET'
    channel VARCHAR(20) NOT NULL,     -- 'EMAIL' ou 'WHATSAPP'
    attempts INT DEFAULT 0,           -- tentativas de validação (limite de 5)
    is_used BOOLEAN DEFAULT FALSE,
    reset_token VARCHAR(255),         -- token temporário para redefinição de senha
    expires_at TIMESTAMPTZ NOT NULL,  -- expiração em 15 minutos
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_codes_lookup ON auth_verification_codes(identifier, purpose, is_used);
CREATE INDEX IF NOT EXISTS idx_auth_codes_token ON auth_verification_codes(reset_token);

-- 22. LOGS DE AUDITORIA DO APLICATIVO (Telemetria LGPD)
CREATE TABLE IF NOT EXISTS audit_logs (
    id           VARCHAR(64)  PRIMARY KEY,
    user_id      VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    event_type   VARCHAR(100) NOT NULL,
    action       VARCHAR(100) NOT NULL,
    metadata     JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id, created_at DESC);

-- 23. COLUNAS FALTANTES EM TABELAS EXISTENTES (migrations de compatibilidade)

-- exams: result_summary e validated_by
ALTER TABLE exams ADD COLUMN IF NOT EXISTS result_summary TEXT;
ALTER TABLE exams ADD COLUMN IF NOT EXISTS validated_by VARCHAR(128);

-- medications: updated_at para detectar mudanças
ALTER TABLE medications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- social_posts: campos de visibilidade e posts familiares
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS visibility VARCHAR(20) NOT NULL DEFAULT 'PUBLIC';
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS is_family_post BOOLEAN DEFAULT FALSE;
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS caregiver_name VARCHAR(255);
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS patient_name VARCHAR(255);

-- league_memberships: dados do participante
ALTER TABLE league_memberships ADD COLUMN IF NOT EXISTS user_name VARCHAR(255);
ALTER TABLE league_memberships ADD COLUMN IF NOT EXISTS user_level INT DEFAULT 1;
ALTER TABLE league_memberships ADD COLUMN IF NOT EXISTS user_streak INT DEFAULT 0;
