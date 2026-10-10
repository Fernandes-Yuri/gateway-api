-- Migração 047: Registro da unidade de venda comercial do ingrediente.
-- O preço continua gravado na unidade canônica do catálogo (kg, L ou un) para
-- preservar app Android, médias da comunidade e enriquecimento. As colunas
-- sale_* guardam COMO o administrador informou o preço (ex.: peça de 300 g),
-- permitindo exibir o valor digitado e o equivalente normalizado no painel.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Colunas de unidade de venda ao lado do preço gravado
ALTER TABLE grocery_price_cache ADD COLUMN IF NOT EXISTS sale_price NUMERIC(10, 2);
ALTER TABLE grocery_price_cache ADD COLUMN IF NOT EXISTS sale_size NUMERIC(12, 3);
ALTER TABLE grocery_price_cache ADD COLUMN IF NOT EXISTS sale_unit TEXT;

-- 2. Restrição de unidade de venda por dimensão da unidade canônica
--    kg aceita g ou kg; L aceita ml ou L; un aceita un ou dz (dúzia).
ALTER TABLE grocery_price_cache DROP CONSTRAINT IF EXISTS grocery_price_cache_sale_unit_check;
ALTER TABLE grocery_price_cache ADD CONSTRAINT grocery_price_cache_sale_unit_check
  CHECK (
    sale_unit IS NULL OR (
      (price_unit = 'kg' AND sale_unit IN ('g', 'kg')) OR
      (price_unit = 'L' AND sale_unit IN ('ml', 'L')) OR
      (price_unit = 'un' AND sale_unit IN ('un', 'dz'))
    )
  );

-- 3. Unidade de venda sem tamanho não faz sentido: os três campos andam juntos
ALTER TABLE grocery_price_cache DROP CONSTRAINT IF NOT EXISTS grocery_price_cache_sale_triplet_check;
ALTER TABLE grocery_price_cache ADD CONSTRAINT grocery_price_cache_sale_triplet_check
  CHECK (
    (sale_price IS NULL AND sale_size IS NULL AND sale_unit IS NULL) OR
    (sale_price IS NOT NULL AND sale_size > 0 AND sale_unit IS NOT NULL)
  );

-- 4. Índice para o painel listar itens com embalagem registrada
CREATE INDEX IF NOT EXISTS grocery_price_cache_sale_unit_idx
  ON grocery_price_cache(ingredient_slug) WHERE sale_unit IS NOT NULL;

-- 5. Revisão do catálogo para o app baixar o snapshot de novo
UPDATE nutrition_catalog_settings SET revision = revision + 1 WHERE id = 1;

COMMIT;
