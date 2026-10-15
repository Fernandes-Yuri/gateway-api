BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE grocery_publication_state (
 id INTEGER PRIMARY KEY CHECK(id=1), revision BIGINT NOT NULL DEFAULT 0 CHECK(revision>=0)
);
INSERT INTO grocery_publication_state(id) VALUES(1);
CREATE TABLE grocery_publications (
 id TEXT PRIMARY KEY, version BIGINT NOT NULL UNIQUE,
 edition_id TEXT NOT NULL REFERENCES grocery_editions(id), source_revision INTEGER NOT NULL,
 published_at TIMESTAMPTZ NOT NULL DEFAULT now(), starts_on DATE NOT NULL, ends_on DATE NOT NULL,
 snapshot JSONB NOT NULL, request_fingerprint TEXT NOT NULL, CHECK(ends_on>=starts_on), UNIQUE(edition_id,id)
);
CREATE INDEX grocery_publications_edition_idx ON grocery_publications(edition_id,version DESC);
CREATE TABLE grocery_publication_heads (
 edition_id TEXT PRIMARY KEY REFERENCES grocery_editions(id), publication_id TEXT,
 FOREIGN KEY(edition_id,publication_id) REFERENCES grocery_publications(edition_id,id)
);
-- Publication bodies are append-only. Withdrawal/restoration changes only the head.
CREATE FUNCTION grocery_publication_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Published grocery snapshots are immutable';
END;
$$;
CREATE TRIGGER grocery_publication_immutable BEFORE UPDATE OR DELETE ON grocery_publications
 FOR EACH ROW EXECUTE FUNCTION grocery_publication_immutable();
COMMIT;
