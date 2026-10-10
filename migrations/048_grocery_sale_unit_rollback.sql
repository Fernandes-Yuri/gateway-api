-- Rollback da migração 048: remove o registro da unidade de venda comercial.
-- Só apaga as colunas sale_*; o preço normalizado na unidade canônica não é
-- tocado, então nada do que o app e a comunidade consomem se perde.
BEGIN;

SET LOCAL lock_timeout = '10s';

DROP INDEX IF EXISTS grocery_price_cache_sale_unit_idx;
ALTER TABLE grocery_price_cache DROP CONSTRAINT IF EXISTS grocery_price_cache_sale_triplet_check;
ALTER TABLE grocery_price_cache DROP CONSTRAINT IF EXISTS grocery_price_cache_sale_unit_check;
ALTER TABLE grocery_price_cache DROP COLUMN IF EXISTS sale_unit;
ALTER TABLE grocery_price_cache DROP COLUMN IF EXISTS sale_size;
ALTER TABLE grocery_price_cache DROP COLUMN IF EXISTS sale_price;

UPDATE nutrition_catalog_settings SET revision = revision + 1 WHERE id = 1;

COMMIT;
