-- Atendimento interno WhatsApp. Sem seed de operador ou credenciais.
CREATE TABLE whatsapp_intake_operators (
    uid TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT 'Yuri',
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE whatsapp_intake_conversations (
    id UUID PRIMARY KEY,
    receiver TEXT NOT NULL,
    phone TEXT NOT NULL,
    phone_hash CHAR(64) NOT NULL,
    mode TEXT NOT NULL DEFAULT 'bot' CHECK (mode IN ('bot','waiting_human','human','closed')),
    epoch BIGINT NOT NULL DEFAULT 0,
    operator_uid TEXT,
    intake_status TEXT NOT NULL DEFAULT 'collecting',
    fields JSONB NOT NULL DEFAULT '{}',
    summary TEXT,
    summary_message_id BIGINT,
    consented BOOLEAN NOT NULL DEFAULT FALSE,
    last_inbound_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(receiver,phone_hash)
);
CREATE TABLE whatsapp_intake_inbox (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES whatsapp_intake_conversations(id) ON DELETE CASCADE,
    meta_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','done','failed')),
    attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(conversation_id,meta_id)
);
CREATE TABLE whatsapp_intake_messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES whatsapp_intake_conversations(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK(origin IN ('contact','bot','human','system')),
    kind TEXT NOT NULL CHECK(kind IN ('text','audio','image','unsupported')),
    body TEXT NOT NULL DEFAULT '',
    meta_id TEXT,
    inbox_id BIGINT REFERENCES whatsapp_intake_inbox(id) ON DELETE SET NULL,
    operator_uid TEXT,
    epoch BIGINT NOT NULL,
    idempotency_key TEXT,
    request_hash TEXT,
    delivery TEXT NOT NULL DEFAULT 'received' CHECK(delivery IN ('received','queued','sending','sent','delivered','read','failed','uncertain','cancelled')),
    transcript TEXT,
    transcript_status TEXT NOT NULL DEFAULT 'none' CHECK(transcript_status IN ('none','pending','completed','failed')),
    error_code TEXT,
    sent_at TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(conversation_id,idempotency_key),
    UNIQUE(conversation_id,inbox_id,origin)
);
CREATE TABLE whatsapp_intake_media (
    id UUID PRIMARY KEY,
    message_id BIGINT NOT NULL UNIQUE REFERENCES whatsapp_intake_messages(id) ON DELETE CASCADE,
    mime_type TEXT NOT NULL,
    content BYTEA,
    meta_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready','failed')),
    CHECK(octet_length(content) <= 16777216)
);
-- O produtor de eventos serializa a atribuição do cursor até o commit.
CREATE TABLE whatsapp_intake_events (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL,
    kind TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE whatsapp_intake_commands (
    conversation_id UUID NOT NULL REFERENCES whatsapp_intake_conversations(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    uid TEXT NOT NULL,
    action TEXT NOT NULL,
    epoch BIGINT NOT NULL,
    PRIMARY KEY(conversation_id,key)
);
CREATE TABLE whatsapp_intake_handoffs (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES whatsapp_intake_conversations(id) ON DELETE CASCADE,
    epoch BIGINT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','push_accepted','seen','claimed')),
    attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(conversation_id,epoch)
);
CREATE TABLE whatsapp_intake_devices (
    id UUID PRIMARY KEY,
    uid TEXT NOT NULL REFERENCES whatsapp_intake_operators(uid) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE whatsapp_intake_reads (
    conversation_id UUID NOT NULL REFERENCES whatsapp_intake_conversations(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    message_id BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(conversation_id,uid)
);
CREATE INDEX idx_intake_inbox_work ON whatsapp_intake_inbox(status,available_at,id);
CREATE INDEX idx_intake_message_work ON whatsapp_intake_messages(delivery,available_at,id);
CREATE INDEX idx_intake_message_conversation ON whatsapp_intake_messages(conversation_id,id);
CREATE INDEX idx_intake_media_pending ON whatsapp_intake_media(status);
CREATE INDEX idx_intake_events_conversation ON whatsapp_intake_events(conversation_id,id);
CREATE INDEX idx_intake_updated ON whatsapp_intake_conversations(updated_at DESC,id);

CREATE TABLE whatsapp_intake_consents (
    phone_hash CHAR(64) PRIMARY KEY,
    terms_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL,
    presented_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '10 minutes'
);
CREATE TABLE whatsapp_intake_deletions (
    phone_hash CHAR(64) PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '5 minutes'
);
CREATE TABLE whatsapp_intake_sessions (
    device_id UUID PRIMARY KEY,
    uid TEXT NOT NULL REFERENCES whatsapp_intake_operators(uid) ON DELETE CASCADE,
    access_hash CHAR(64) NOT NULL UNIQUE,
    refresh_hash CHAR(64) NOT NULL UNIQUE,
    access_expires_at TIMESTAMPTZ NOT NULL,
    refresh_expires_at TIMESTAMPTZ NOT NULL,
    master_fingerprint CHAR(64) NOT NULL
);
