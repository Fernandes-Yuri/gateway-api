-- Migração 046: Padronização canônica dos nomes comerciais de produtos
-- Elimina termos de corte, estado e cocção dos nomes em grocery_ingredients
-- e sincroniza grocery_price_cache com os nomes comerciais simples.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Renomeia os 12 ingredientes com termos de preparo/corte para nomes comerciais simples,
-- preservando o nome anterior na lista de sinônimos (details.aliases).
UPDATE grocery_ingredients g
SET name = r.new_name,
    details = jsonb_set(
      g.details,
      '{aliases}',
      CASE
        WHEN g.details->'aliases' ? r.old_name THEN g.details->'aliases'
        ELSE COALESCE(g.details->'aliases', '[]'::jsonb) || jsonb_build_array(r.old_name)
      END
    )
FROM (VALUES
  ('germen-de-trigo-tostado', 'Gérmen de Trigo', 'Gérmen de Trigo Tostado'),
  ('linhaca-dourada-moida', 'Linhaça Dourada', 'Linhaça Dourada Moída'),
  ('linhaca-marrom-moida', 'Linhaça Marrom', 'Linhaça Marrom Moída'),
  ('lombo-suino-assado-magro', 'Lombo Suíno Magro', 'Lombo Suíno Assado Magro'),
  ('macadamia-crua', 'Macadâmia', 'Macadâmia Crua'),
  ('musculo-bovino-moido', 'Músculo Bovino Magro', 'Músculo Bovino Moído'),
  ('patinho-bovino-moido-magro', 'Patinho Bovino', 'Patinho Bovino Moído Magro'),
  ('pimenta-do-reino-moida-na-hora', 'Pimenta-do-Reino', 'Pimenta-do-Reino Moída na Hora'),
  ('rabanete-fatiado-cru', 'Rabanete', 'Rabanete Fatiado Cru'),
  ('repolho-roxo-cru', 'Repolho Roxo', 'Repolho Roxo Cru'),
  ('sardinha-em-conserva-ao-molho-de-tomate', 'Sardinha em Conserva com Molho de Tomate', 'Sardinha em Conserva ao Molho de Tomate'),
  ('sementes-de-abobora-tostadas', 'Sementes de Abóbora', 'Sementes de Abóbora Tostadas')
) AS r(slug, new_name, old_name)
WHERE g.slug = r.slug;

-- 2. Atualiza grocery_price_cache para usar o nome comercial limpo do produto
-- em todas as linhas vinculadas por ingredient_slug, removendo nomes de pratos legados.
UPDATE grocery_price_cache c
SET food_name = g.name,
    search_term = g.name
FROM grocery_ingredients g
WHERE c.ingredient_slug = g.slug
  AND c.food_name <> g.name;

-- 3. Limpeza de auto-sinônimos que possam ter surgido do rename
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

-- 4. Incremento da revisão do catálogo nutricional para que o app baixe os novos nomes
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
