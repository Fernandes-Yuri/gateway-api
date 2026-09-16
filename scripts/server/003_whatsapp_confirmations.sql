-- =============================================================
-- MIGRAÇÃO 003: Sistema de Confirmação WhatsApp
-- Tabelas para fluxo de confirmação de registros de saúde
-- =============================================================

-- 1. Tabela de confirmações pendentes
CREATE TABLE IF NOT EXISTS pending_confirmations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    confirmation_code VARCHAR(20) NOT NULL UNIQUE,
    metric_type     VARCHAR(30) NOT NULL,  -- PRESSAO, GLICEMIA, AGUA, PESO, REMEDIO, ALIMENTO
    value_json      JSONB NOT NULL,        -- {sistolica:130, diastolica:85} ou {glicemia:100}
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, confirmed, cancelled, expired
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    confirmed_at    TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pending_conf_user ON pending_confirmations(user_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_conf_code ON pending_confirmations(confirmation_code);
CREATE INDEX IF NOT EXISTS idx_pending_conf_expires ON pending_confirmations(expires_at) WHERE status = 'pending';

-- 2. Tabela de log de confirmações (auditoria completa)
CREATE TABLE IF NOT EXISTS confirmation_logs (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id           VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    pending_conf_id   UUID REFERENCES pending_confirmations(id),
    confirmation_code VARCHAR(20) NOT NULL,
    action            VARCHAR(50) NOT NULL,  -- REGISTRAR_PRESSAO, REGISTRAR_GLICEMIA, etc.
    values_submitted  JSONB NOT NULL,        -- valores originais do usuário
    values_confirmed  JSONB,                 -- valores finais (pós edição se houver)
    channel           VARCHAR(20) DEFAULT 'whatsapp',  -- whatsapp, app, web
    confirmed_by_user BOOLEAN DEFAULT TRUE,
    was_edited        BOOLEAN DEFAULT FALSE,
    ip_address        INET,
    user_agent        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conf_logs_user ON confirmation_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conf_logs_code ON confirmation_logs(confirmation_code);

-- 3. Adicionar colunas na tabela vital_signs existente
DO $$
BEGIN
    -- Coluna para canal de confirmação
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'vital_signs' AND column_name = 'confirmed_via') THEN
        ALTER TABLE vital_signs ADD COLUMN confirmed_via VARCHAR(20) DEFAULT 'app';
    END IF;

    -- Coluna para código de confirmação
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'vital_signs' AND column_name = 'confirmation_code') THEN
        ALTER TABLE vital_signs ADD COLUMN confirmation_code VARCHAR(20);
    END IF;
END $$;

-- 4. Índices para vital_signs com as novas colunas
CREATE INDEX IF NOT EXISTS idx_vitals_confirmation ON vital_signs(confirmation_code) WHERE confirmation_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vitals_confirmed_via ON vital_signs(confirmed_via);

-- 5. Função para expirar confirmações antigas
CREATE OR REPLACE FUNCTION expire_pending_confirmations()
RETURNS void AS $$
BEGIN
    UPDATE pending_confirmations
    SET status = 'expired'
    WHERE status = 'pending'
      AND expires_at < NOW();
END;
$$ LANGUAGE plpgsql;

-- 6. View para relatório de confirmações
CREATE OR REPLACE VIEW v_confirmation_report AS
SELECT
    cl.id,
    cl.user_id,
    p.name AS user_name,
    cl.confirmation_code,
    cl.action,
    cl.values_confirmed,
    cl.channel,
    cl.confirmed_by_user,
    cl.was_edited,
    cl.created_at,
    cl.created_at - pc.created_at AS time_to_confirm
FROM confirmation_logs cl
LEFT JOIN pending_confirmations pc ON cl.pending_conf_id = pc.id
LEFT JOIN profiles p ON cl.user_id = p.id
WHERE cl.confirmed_by_user = TRUE
ORDER BY cl.created_at DESC;

COMMENT ON TABLE pending_confirmations IS 'Registros de saúde aguardando confirmação do usuário via WhatsApp';
COMMENT ON TABLE confirmation_logs IS 'Log de auditoria de confirmações de registros de saúde';
COMMENT ON COLUMN vital_signs.confirmed_via IS 'Canal de confirmação: whatsapp, app, web';
COMMENT ON COLUMN vital_signs.confirmation_code IS 'Código único de confirmação para auditoria';
