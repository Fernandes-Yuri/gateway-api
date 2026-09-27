-- 022_community_posts.sql
-- Feed Comunitario — Publicacoes de Orgulho Familiar (02_PWA_CUIDADOR/01 §4).
-- visibility='community': visivel para toda a comunidade Braga Saude.
-- visibility='family': restrito ao Circulo Familiar (titular + cuidadores ACTIVE).
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

CREATE TABLE IF NOT EXISTS community_posts (
    id uuid PRIMARY KEY,
    patient_id text NOT NULL,
    author_user_id text NOT NULL,
    author_name varchar(100) NOT NULL DEFAULT '',
    author_relation varchar(50) NOT NULL DEFAULT '',
    content varchar(500) NOT NULL,
    type varchar(20) NOT NULL DEFAULT 'achievement',
    visibility varchar(10) NOT NULL DEFAULT 'community',
    likes_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    CONSTRAINT community_posts_type_check CHECK (type IN ('achievement','recipe','walk','milestone','tip')),
    CONSTRAINT community_posts_visibility_check CHECK (visibility IN ('community','family'))
);

CREATE TABLE IF NOT EXISTS community_post_likes (
    post_id uuid NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
    user_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_community_posts_visibility_created
    ON community_posts (visibility, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_community_posts_patient_created
    ON community_posts (patient_id, created_at DESC);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('022_community_posts', 'community_posts', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
