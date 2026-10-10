-- Rollback da migração 045: restaura linhas arquivadas do cache de preços,
-- remove ingredient_slug e reverte shoppingComponents dos alimentos 221-240.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Restaura linhas excluídas e deduplicadas do cache a partir da tabela de histórico
INSERT INTO grocery_price_cache (food_name, search_term, price_avg, price_min, price_max, source, price_unit, updated_at)
SELECT food_name, food_name, price_avg, price_min, price_max, source, price_unit, archived_at
FROM grocery_price_cache_history
ON CONFLICT (food_name) DO UPDATE SET
  price_avg = EXCLUDED.price_avg,
  price_min = EXCLUDED.price_min,
  price_max = EXCLUDED.price_max,
  source = EXCLUDED.source,
  price_unit = EXCLUDED.price_unit,
  updated_at = EXCLUDED.updated_at;

-- 2. Remove índice e coluna ingredient_slug
DROP INDEX IF EXISTS grocery_price_cache_ingredient_slug_idx;
ALTER TABLE grocery_price_cache DROP COLUMN IF EXISTS ingredient_slug;

-- 3. Remove tabela de histórico
DROP TABLE IF EXISTS grocery_price_cache_history;

-- 4. Reverte default de source para meli
ALTER TABLE grocery_price_cache ALTER COLUMN source SET DEFAULT 'meli';

-- 5. Reverte shoppingComponents e purchaseFactors adicionados aos alimentos de café/lanche
UPDATE food_catalog
SET details = details - 'shoppingComponents' - 'purchaseFactors'
WHERE remote_id BETWEEN 'food_221' AND 'food_240';

-- 6. Incrementa a revisão para invalidar cache de clientes
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
