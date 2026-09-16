-- =============================================================
-- MIGRAÃ‡ÃƒO 005: Mensagens familiares efÃªmeras (D47 â€” 16/09/2026)
-- RetenÃ§Ã£o mÃ¡xima de 24h: TTL individual (expires_at) e tombstone
-- de exclusÃ£o pelo usuÃ¡rio (deleted_at). Nenhum resÃ­duo ultrapassa
-- a janela de acesso de 24h. Limpeza fÃ­sica a cada 30s com serviÃ§o ativo.
-- =============================================================

ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE family_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Backfill: mensagens legadas expiram 24h apÃ³s o envio original
UPDATE family_messages SET expires_at = sent_at + INTERVAL '24 hours' WHERE expires_at IS NULL;

ALTER TABLE family_messages ALTER COLUMN expires_at SET NOT NULL;
ALTER TABLE family_messages ALTER COLUMN expires_at SET DEFAULT (NOW() + INTERVAL '24 hours');

CREATE INDEX IF NOT EXISTS idx_family_messages_expiry ON family_messages(expires_at);

COMMENT ON COLUMN family_messages.expires_at IS 'D47: mensagem some das consultas apÃ³s este instante (sent_at + 24h)';
COMMENT ON COLUMN family_messages.deleted_at IS 'D47: tombstone de exclusÃ£o pelo remetente (LGPD Art. 18, VI)';

UPDATE family_messages SET message_text='', sender_name='', icon_type='LOVE' WHERE deleted_at IS NOT NULL;
DELETE FROM family_messages WHERE expires_at <= now();

INSERT INTO schema_migrations(version) VALUES ('006_family_messages_retention') ON CONFLICT DO NOTHING;

