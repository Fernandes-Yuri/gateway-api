-- Migração 045: Deduplicação do cache de preços, chaveamento por ingredient_slug,
-- aposentadoria do Mercado Livre e saneamento das pontes de café/lanches.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Aposentadoria de meli: converte para estimated e ajusta default da coluna
UPDATE grocery_price_cache
SET source = 'estimated'
WHERE source = 'meli';

ALTER TABLE grocery_price_cache ALTER COLUMN source SET DEFAULT 'manual';

-- 2. Novo campo ingredient_slug em grocery_price_cache
ALTER TABLE grocery_price_cache ADD COLUMN IF NOT EXISTS ingredient_slug TEXT REFERENCES grocery_ingredients(slug);
CREATE INDEX IF NOT EXISTS grocery_price_cache_ingredient_slug_idx ON grocery_price_cache(ingredient_slug);

-- 3. Tabela de auditoria / arquivo histórico para dados mesclados ou substituídos
CREATE TABLE IF NOT EXISTS grocery_price_cache_history (
  id BIGSERIAL PRIMARY KEY,
  food_name TEXT NOT NULL,
  ingredient_slug TEXT,
  price_avg NUMERIC(10, 2),
  price_min NUMERIC(10, 2),
  price_max NUMERIC(10, 2),
  source TEXT,
  price_unit TEXT,
  archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reason TEXT NOT NULL DEFAULT 'deduplication_migration_045'
);

-- 4. Backfill de ingredient_slug a partir de grocery_ingredients

-- 4a. Casamento direto por slug exato
UPDATE grocery_price_cache c
SET ingredient_slug = g.slug
FROM grocery_ingredients g
WHERE c.ingredient_slug IS NULL
  AND c.food_name = g.slug;

-- 4b. Casamento por nome exato (case-insensitive e sem espaços extras)
UPDATE grocery_price_cache c
SET ingredient_slug = g.slug
FROM grocery_ingredients g
WHERE c.ingredient_slug IS NULL
  AND lower(trim(c.food_name)) = lower(trim(g.name));

-- 4c. Casamento pelos aliases declarados no details do ingrediente
UPDATE grocery_price_cache c
SET ingredient_slug = g.slug
FROM grocery_ingredients g
WHERE c.ingredient_slug IS NULL
  AND g.details ? 'aliases'
  AND EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(g.details->'aliases') AS alias
    WHERE lower(trim(alias)) = lower(trim(c.food_name))
  );

-- 4d. Casamento através da ponte food_ids / food_catalog
UPDATE grocery_price_cache c
SET ingredient_slug = g.slug
FROM grocery_ingredients g
JOIN food_catalog f ON lower(trim(f.name)) = lower(trim(c.food_name))
WHERE c.ingredient_slug IS NULL
  AND g.details ? 'food_ids'
  AND g.details->'food_ids' ? f.remote_id;

-- 4e. Casamento através de shoppingComponents em food_catalog
UPDATE grocery_price_cache c
SET ingredient_slug = s.component
FROM food_catalog f,
     jsonb_array_elements_text(f.details->'shoppingComponents') AS s(component)
JOIN grocery_ingredients g ON g.slug = s.component
WHERE c.ingredient_slug IS NULL
  AND lower(trim(c.food_name)) = lower(trim(f.name));

-- 4f. Casamento por search_term igual a slug ou nome
UPDATE grocery_price_cache c
SET ingredient_slug = g.slug
FROM grocery_ingredients g
WHERE c.ingredient_slug IS NULL
  AND (c.search_term = g.slug OR lower(trim(c.search_term)) = lower(trim(g.name)));

-- 5. Exclusão e arquivamento da linha morta ("Água Saborizada...")
INSERT INTO grocery_price_cache_history (food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit, reason)
SELECT food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit, 'dead_row_removal'
FROM grocery_price_cache
WHERE food_name ILIKE '%Água Saborizada com Pepino%';

DELETE FROM grocery_price_cache
WHERE food_name ILIKE '%Água Saborizada com Pepino%';

-- 6. Arquivamento e deduplicação de linhas com o mesmo ingredient_slug
WITH ranked AS (
  SELECT c.food_name, c.ingredient_slug, c.price_avg, c.price_min, c.price_max, c.source, c.price_unit, c.updated_at,
         ROW_NUMBER() OVER (
           PARTITION BY c.ingredient_slug
           ORDER BY
             CASE WHEN c.source = 'manual' THEN 1 ELSE 2 END,
             CASE WHEN lower(trim(c.food_name)) = lower(trim(g.name)) THEN 1 ELSE 2 END,
             c.updated_at DESC NULLS LAST
         ) AS rn
  FROM grocery_price_cache c
  JOIN grocery_ingredients g ON g.slug = c.ingredient_slug
  WHERE c.ingredient_slug IS NOT NULL
),
duplicates AS (
  SELECT * FROM ranked WHERE rn > 1
)
INSERT INTO grocery_price_cache_history (food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit, reason)
SELECT food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit, 'deduplication_migration_045'
FROM duplicates;

WITH ranked AS (
  SELECT c.food_name, c.ingredient_slug,
         ROW_NUMBER() OVER (
           PARTITION BY c.ingredient_slug
           ORDER BY
             CASE WHEN c.source = 'manual' THEN 1 ELSE 2 END,
             CASE WHEN lower(trim(c.food_name)) = lower(trim(g.name)) THEN 1 ELSE 2 END,
             c.updated_at DESC NULLS LAST
         ) AS rn
  FROM grocery_price_cache c
  JOIN grocery_ingredients g ON g.slug = c.ingredient_slug
  WHERE c.ingredient_slug IS NOT NULL
)
DELETE FROM grocery_price_cache
WHERE food_name IN (SELECT food_name FROM ranked WHERE rn > 1);

-- 7. Remoção de auto-sinônimos em grocery_ingredients (onde alias == name)
UPDATE grocery_ingredients
SET details = jsonb_set(
  details,
  '{aliases}',
  COALESCE((
    SELECT jsonb_agg(alias)
    FROM jsonb_array_elements_text(details->'aliases') AS alias
    WHERE lower(trim(alias)) <> lower(trim(name))
  ), '[]'::jsonb)
)
WHERE details ? 'aliases'
  AND EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(details->'aliases') AS alias
    WHERE lower(trim(alias)) = lower(trim(name))
  );

-- 8. Correção de shoppingComponents para os 20 alimentos de café e lanches da 043 (food_221 a food_240)
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['pao-frances'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_221';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['pao-de-forma-integral'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_222';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['goma-de-tapioca'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_223';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['goma-de-tapioca', 'ovo-caipira'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_224';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['aveia-em-flocos-finos', 'banana-prata', 'ovo-caipira'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_225';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['aveia-em-flocos-finos', 'leite-desnatado-pasteurizado'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_226';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['torrada-integral'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_227';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['pao-de-queijo'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_228';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['milho-para-pipoca'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_229';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['biscoito-de-polvilho'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_230';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['polenta-de-milho', 'ovo-caipira', 'leite-desnatado-pasteurizado'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_231';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['ovo-caipira'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_232';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['requeijao-cremoso'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_233';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['manteiga-com-sal'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_234';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['queijo-mussarela'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_235';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['peito-de-frango'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_236';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['atum-ralado-ao-natural'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_237';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['po-de-cafe-tradicional'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_238';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['po-de-cafe-tradicional', 'leite-desnatado-pasteurizado'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_239';
UPDATE food_catalog SET details = details || jsonb_build_object('shoppingComponents', ARRAY['banana-prata', 'leite-desnatado-pasteurizado', 'aveia-em-flocos-finos'], 'purchaseFactors', '{}'::jsonb) WHERE remote_id = 'food_240';

-- 9. Incremento da revisão do catálogo para forçar sincronização fresca no app
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
