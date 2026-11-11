CREATE INDEX IF NOT EXISTS idx_family_messages_purge
ON family_messages (expires_at)
WHERE deleted_at IS NULL;
