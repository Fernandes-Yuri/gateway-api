DROP TABLE IF EXISTS grocery_price_cache;
-- Duas identificações porque esta migration se registou duas vezes:
-- o alias '026' (compatibilidade, escrito pela própria migration) e o
-- stem que o migrate.py grava ao aplicá-la. Apagar só o alias deixava o
-- runner a considerar a migration aplicada depois do rollback.
DELETE FROM schema_migrations WHERE version IN ('026', '026_grocery_price_cache');
