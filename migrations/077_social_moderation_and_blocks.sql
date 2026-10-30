BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS social_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    reported_by VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    author_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reason VARCHAR(64) NOT NULL CHECK (reason IN ('offensive', 'spam', 'misinformation', 'other')),
    details TEXT CHECK (char_length(details) <= 2000),
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dismissed', 'removed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by VARCHAR(128),
    UNIQUE (post_id, reported_by),
    CHECK (reported_by <> author_id)
);
CREATE INDEX IF NOT EXISTS idx_social_reports_status ON social_reports(status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_social_reports_reporter ON social_reports(reported_by);
CREATE INDEX IF NOT EXISTS idx_social_reports_author ON social_reports(author_id);

CREATE TABLE IF NOT EXISTS social_user_blocks (
    user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    blocked_user_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, blocked_user_id),
    CHECK (user_id <> blocked_user_id)
);
CREATE INDEX IF NOT EXISTS idx_social_user_blocks_target ON social_user_blocks(blocked_user_id, user_id);
COMMIT;
