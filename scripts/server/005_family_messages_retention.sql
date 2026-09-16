-- =============================================================
-- MIGRAÇÃO 005: Mensagens familiares efêmeras (D47 — 16/09/2026)
-- Retenção máxima de 24h: TTL individual (expires_at) e tombstone
-- de exclusão pelo usuário (deleted_at). Nenhum resíduo ultrapassa
-- a janela de acesso de 24h. Limpeza física a cada 30s com serviço ativo.
-- =============================================================

ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Backfill: mensagens legadas expiram 24h após o envio original
UPDATE family_messages SET expires_at = sent_at + INTERVAL '24 hours' WHERE expires_at IS NULL;

ALTER TABLE family_messages ALTER COLUMN expires_at SET NOT NULL;
ALTER TABLE family_messages ALTER COLUMN expires_at SET DEFAULT (NOW() + INTERVAL '24 hours');

CREATE INDEX IF NOT EXISTS idx_family_messages_expiry ON family_messages(expires_at);

COMMENT ON COLUMN family_messages.expires_at IS 'D47: mensagem some das consultas após este instante (sent_at + 24h)';
COMMENT ON COLUMN family_messages.deleted_at IS 'D47: tombstone de exclusão pelo remetente (LGPD Art. 18, VI)';

UPDATE family_messages SET message_text='', sender_name='', icon_type='LOVE' WHERE deleted_at IS NOT NULL;
DELETE FROM family_messages WHERE expires_at <= now();
