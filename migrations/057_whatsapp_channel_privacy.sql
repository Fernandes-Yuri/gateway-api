BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE whatsapp_channel_consents (
    phone_hash CHAR(64) PRIMARY KEY,
    terms_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL,
    challenge UUID NOT NULL,
    presented_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '10 minutes'
);
CREATE TABLE whatsapp_channel_deletions (
    phone_hash CHAR(64) PRIMARY KEY,
    actor_id VARCHAR(128) REFERENCES profiles(id) ON DELETE CASCADE,
    challenge UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '5 minutes'
);
ALTER TABLE whatsapp_message_receipts ADD COLUMN phone_hash CHAR(64);
CREATE INDEX idx_wa_receipts_phone_hash ON whatsapp_message_receipts(phone_hash);
ALTER TABLE pending_confirmations ADD COLUMN source_channel VARCHAR(20);
ALTER TABLE pending_confirmations ADD COLUMN channel_actor_id VARCHAR(128);
ALTER TABLE pending_confirmations ADD COLUMN channel_phone_hash CHAR(64);
CREATE INDEX idx_pending_wa_phone_hash ON pending_confirmations(channel_phone_hash);
-- Every production producer of pending_confirmations is a WhatsApp executor
-- (health, medication_take, care_messages, wa_preferences). App preferences
-- use assistant_preference_pending, not this table. Preserve app settings.
UPDATE pending_confirmations SET source_channel='whatsapp',channel_actor_id=user_id
WHERE source_channel IS NULL;
DELETE FROM assistant_preference_pending a USING pending_confirmations p
WHERE p.user_id=a.user_id AND p.metric_type='PREFERENCE'
AND p.value_json->>'proposal_id'=a.proposal_id AND p.source_channel='whatsapp';
UPDATE pending_confirmations SET status='cancelled',cancelled_at=NOW()
WHERE metric_type='PREFERENCE' AND source_channel='whatsapp' AND status='pending';
CREATE TABLE whatsapp_channel_cleanup (
    phone_hash CHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO schema_migrations(version) VALUES ('057_whatsapp_channel_privacy') ON CONFLICT DO NOTHING;
COMMIT;
