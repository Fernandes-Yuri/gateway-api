-- D62: contratos canônicos do Care OS. Compatível com PostgreSQL 14+.
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE family_bindings
    ADD COLUMN IF NOT EXISTS caregiver_role text NOT NULL DEFAULT 'CAREGIVER_VIEWER',
    ADD COLUMN IF NOT EXISTS permissions text[] NOT NULL DEFAULT '{}';

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='family_binding_care_role_check') THEN
        ALTER TABLE family_bindings ADD CONSTRAINT family_binding_care_role_check
            CHECK (caregiver_role IN ('ADMIN_CHILD','CAREGIVER_VIEWER','PROFESSIONAL_NURSE')) NOT VALID;
    END IF;
END $$;

ALTER TABLE medications
    ADD COLUMN IF NOT EXISTS ean_barcode varchar(13),
    ADD COLUMN IF NOT EXISTS active_principle varchar(150),
    ADD COLUMN IF NOT EXISTS manufacturer varchar(100),
    ADD COLUMN IF NOT EXISTS pharmaceutical_form varchar(100),
    ADD COLUMN IF NOT EXISTS total_units integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS current_units integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS alert_threshold_days integer NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS photo_reference_url varchar(500),
    ADD COLUMN IF NOT EXISTS confirmed_with_prescription boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS last_restock_date timestamptz;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='medications_stock_nonnegative') THEN
        ALTER TABLE medications ADD CONSTRAINT medications_stock_nonnegative
            CHECK (total_units >= 0 AND current_units >= 0 AND alert_threshold_days BETWEEN 1 AND 30) NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS prescriptions (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    medication_id uuid NOT NULL UNIQUE REFERENCES medications(id) ON DELETE CASCADE,
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    image_url varchar(500),
    issued_on date NOT NULL,
    validity_days integer NOT NULL CHECK (validity_days BETWEEN 1 AND 365),
    prescriber_name varchar(150) NOT NULL,
    prescriber_crm varchar(32) NOT NULL,
    confirmed_by varchar(128) NOT NULL REFERENCES profiles(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS anvisa_medication_catalog (
    ean_barcode varchar(13) PRIMARY KEY CHECK (ean_barcode ~ '^[0-9]{13}$'),
    name varchar(255) NOT NULL,
    active_principle varchar(150),
    concentration varchar(80),
    pharmaceutical_form varchar(100),
    manufacturer varchar(100),
    farmacia_popular_eligible boolean,
    source_name varchar(120) NOT NULL,
    source_url varchar(500),
    source_checked_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS care_audit_trail (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    actor_id varchar(128) NOT NULL REFERENCES profiles(id),
    actor_name varchar(255) NOT NULL,
    action_type varchar(80) NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_care_audit_patient_time ON care_audit_trail(patient_id, occurred_at DESC);

ALTER TABLE medication_logs
    ADD COLUMN IF NOT EXISTS actor_id varchar(128) REFERENCES profiles(id),
    ADD COLUMN IF NOT EXISTS units_taken integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS idempotency_key varchar(100);
CREATE UNIQUE INDEX IF NOT EXISTS medication_logs_idempotency
    ON medication_logs(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS medication_stock_adjustments (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    medication_id uuid NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    actor_id varchar(128) NOT NULL REFERENCES profiles(id),
    new_total_units integer NOT NULL CHECK (new_total_units BETWEEN 1 AND 10000),
    idempotency_key varchar(100) NOT NULL,
    adjusted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(patient_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_stock_adjustments_medication_time
    ON medication_stock_adjustments(medication_id, adjusted_at DESC);

CREATE TABLE IF NOT EXISTS symptoms_diary (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reported_by varchar(128) NOT NULL REFERENCES profiles(id),
    reported_at timestamptz NOT NULL,
    symptoms_text varchar(1000) NOT NULL,
    sleep_quality smallint CHECK (sleep_quality BETWEEN 1 AND 5),
    disposition smallint CHECK (disposition BETWEEN 1 AND 5),
    input_method varchar(10) NOT NULL CHECK (input_method IN ('VOICE','TEXT')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_symptoms_patient_time ON symptoms_diary(patient_id, reported_at DESC);

CREATE TABLE IF NOT EXISTS medical_access_tokens (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL UNIQUE,
    created_by varchar(128) NOT NULL REFERENCES profiles(id),
    purpose varchar(30) NOT NULL DEFAULT 'CONSULTATION',
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_accessed_at timestamptz
);

CREATE TABLE IF NOT EXISTS medical_access_audit (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_id uuid NOT NULL REFERENCES medical_access_tokens(id) ON DELETE CASCADE,
    accessed_at timestamptz NOT NULL DEFAULT now(),
    ip_address varchar(64),
    user_agent varchar(500)
);

CREATE TABLE IF NOT EXISTS emergency_access_tokens (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL UNIQUE,
    created_by varchar(128) NOT NULL REFERENCES profiles(id),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS blood_type varchar(4),
    ADD COLUMN IF NOT EXISTS medication_allergies text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS implants text[] NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS ble_telemetry_receipts (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    device_id varchar(128) NOT NULL,
    device_type varchar(30) NOT NULL CHECK (device_type IN ('BLOOD_PRESSURE','GLUCOSE')),
    protocol varchar(10) NOT NULL DEFAULT 'GATT' CHECK (protocol='GATT'),
    measured_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(patient_id, device_id, measured_at)
);

CREATE TABLE IF NOT EXISTS care_notification_deliveries (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    caregiver_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    notification_type varchar(40) NOT NULL,
    dedupe_key varchar(160) NOT NULL UNIQUE,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

INSERT INTO schema_migrations(version) VALUES ('017_care_os_global_contracts') ON CONFLICT DO NOTHING;
COMMIT;
