-- =============================================================
-- MIGRAÇÃO 004: Ações do agente (recibos de executores)
-- Toda promessa do assistente nasce do ACTION_CATALOG (orb_actions.py);
-- cada execução deixa recibo auditável com chave de idempotência.
-- =============================================================

CREATE TABLE IF NOT EXISTS agent_actions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    channel         VARCHAR(20) NOT NULL,   -- chat, voice, whatsapp
    action          VARCHAR(50) NOT NULL,   -- chave do ACTION_CATALOG
    params          JSONB NOT NULL DEFAULT '{}',
    idempotency_key VARCHAR(64) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'proposed', -- proposed, confirmed, failed
    result          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_user ON agent_actions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_action ON agent_actions(action, created_at DESC);

COMMENT ON TABLE agent_actions IS 'Recibos de ações executadas pelos canais do assistente (agente B)';
