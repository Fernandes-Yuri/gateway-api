-- 023_social_posts_visibility.sql
-- ============================================================
-- NOTA DE EQUIVOCO (27/09/2026): a migration 022_community_posts
-- criou por engano as tabelas community_posts/community_post_likes
-- e o router community.py, duplicando o feed que ja existia.
-- O mural real da comunidade e o social_posts (feed social global,
-- escrito pelo app Android via POST /api/sync/social-post e lido
-- pelo GET /api/social/feed). Ter duas fontes paralelas deixou as
-- publicacoes existentes invisiveis para o aplicativo Android.
-- Correcao: exclui as tabelas equivocadas (a 022 foi removida do
-- repositorio) e implementa de verdade a visibilidade no mural
-- social, que antes nao existia no servidor.
-- ============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

-- 1) Remove o registro e as tabelas da migration equivocada.
DELETE FROM schema_migrations WHERE version = '022_community_posts';
DROP TABLE IF EXISTS community_post_likes;
DROP TABLE IF EXISTS community_posts;

-- 2) Visibilidade real no mural social (o Android ja tem o seletor
--    PUBLIC/FAMILY na UI, mas nunca enviava ao servidor — o DTO passa
--    a aceitar e o default PUBLIC mantem compatibilidade total).
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS visibility varchar(10) NOT NULL DEFAULT 'PUBLIC';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'social_posts_visibility_check'
    ) THEN
        ALTER TABLE social_posts
            ADD CONSTRAINT social_posts_visibility_check
            CHECK (visibility IN ('PUBLIC', 'FAMILY'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_social_posts_visibility_created
    ON social_posts (visibility, created_at DESC);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('023_social_posts_visibility', 'social_posts_visibility', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
