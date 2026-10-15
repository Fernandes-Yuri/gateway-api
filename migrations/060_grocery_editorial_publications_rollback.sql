BEGIN;
DROP TABLE grocery_publication_heads;
DROP TABLE grocery_publications;
DROP FUNCTION grocery_publication_immutable();
DROP TABLE grocery_publication_state;
COMMIT;
