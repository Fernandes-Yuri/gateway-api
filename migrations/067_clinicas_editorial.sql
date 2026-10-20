-- Editorial separado dos dados clínicos e das permissões administrativas gerais.
CREATE TABLE IF NOT EXISTS editorial_members (
 email TEXT PRIMARY KEY REFERENCES admin_users(email),
 role TEXT NOT NULL CHECK (role IN ('writer','publisher')),
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS editorial_articles (
 id UUID PRIMARY KEY,
 owner_email TEXT NOT NULL REFERENCES admin_users(email),
 slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
 draft JSONB NOT NULL,
 published JSONB,
 state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','published','archived')),
 version INTEGER NOT NULL DEFAULT 1,
 published_at TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS editorial_public_feed ON editorial_articles(published_at DESC,id) WHERE state='published';
CREATE TABLE IF NOT EXISTS editorial_events (
 id BIGSERIAL PRIMARY KEY,
 article_id UUID REFERENCES editorial_articles(id),
 actor_email TEXT NOT NULL,
 action TEXT NOT NULL,
 version INTEGER,
 snapshot JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS editorial_media (
 id UUID PRIMARY KEY,
 article_id UUID NOT NULL REFERENCES editorial_articles(id),
 content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
 data BYTEA NOT NULL CHECK (octet_length(data) <= 2097152),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='bragasaude_app') THEN
  GRANT SELECT, INSERT, UPDATE ON editorial_members,editorial_articles,editorial_events,editorial_media TO bragasaude_app;
  GRANT USAGE, SELECT ON SEQUENCE editorial_events_id_seq TO bragasaude_app;
 END IF;
END $$;
