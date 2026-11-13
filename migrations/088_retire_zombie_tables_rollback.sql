-- =============================================================
-- ROLLBACK 088: Recria tabelas zumbis vazias se necessario
-- =============================================================

CREATE TABLE IF NOT EXISTS public.biometries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    weight DOUBLE PRECISION NOT NULL,
    height DOUBLE PRECISION NOT NULL,
    imc DOUBLE PRECISION NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_biometries_user_measured ON public.biometries(user_id, measured_at DESC);

CREATE TABLE IF NOT EXISTS public.pending_confirmations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    confirmation_code VARCHAR(20) NOT NULL UNIQUE,
    metric_type VARCHAR(30) NOT NULL,
    value_json JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    source_channel VARCHAR(20),
    channel_actor_id VARCHAR(128),
    channel_phone_hash CHAR(64)
);

CREATE TABLE IF NOT EXISTS public.confirmation_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    pending_conf_id UUID REFERENCES pending_confirmations(id),
    confirmation_code VARCHAR(20) NOT NULL,
    action VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
