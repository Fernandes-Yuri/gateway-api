-- =============================================================
-- MIGRAÇÃO 010: Respostas a feedbacks (doc 10 §4.1 — 18/09/2026)
-- A equipe responde um feedback; o app puxa a resposta no SyncWorker
-- e mostra uma notificação local ao usuário.
-- =============================================================

CREATE TABLE IF NOT EXISTS feedback_replies (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_id UUID NOT NULL REFERENCES feedbacks(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT 'Suporte Braga Saúde',
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Pull incremental: o app só puxa o que ainda não viu.
    delivered_at TIMESTAMPTZ
);

-- Índice para o pull por usuário (apenas não entregues ainda).
CREATE INDEX IF NOT EXISTS idx_feedback_replies_user_undelivered
    ON feedback_replies (user_id, delivered_at)
    WHERE delivered_at IS NULL;
