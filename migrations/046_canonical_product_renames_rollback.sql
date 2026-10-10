-- Rollback da migração 046: restaura nomes anteriores dos ingredientes
-- e incrementa a revisão para propagar de volta aos clientes.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Restaura os nomes originais dos 12 ingredientes
UPDATE grocery_ingredients g
SET name = r.old_name
FROM (VALUES
  ('germen-de-trigo-tostado', 'Gérmen de Trigo Tostado'),
  ('linhaca-dourada-moida', 'Linhaça Dourada Moída'),
  ('linhaca-marrom-moida', 'Linhaça Marrom Moída'),
  ('lombo-suino-assado-magro', 'Lombo Suíno Assado Magro'),
  ('macadamia-crua', 'Macadâmia Crua'),
  ('musculo-bovino-moido', 'Músculo Bovino Moído'),
  ('patinho-bovino-moido-magro', 'Patinho Bovino Moído Magro'),
  ('pimenta-do-reino-moida-na-hora', 'Pimenta-do-Reino Moída na Hora'),
  ('rabanete-fatiado-cru', 'Rabanete Fatiado Cru'),
  ('repolho-roxo-cru', 'Repolho Roxo Cru'),
  ('sardinha-em-conserva-ao-molho-de-tomate', 'Sardinha em Conserva ao Molho de Tomate'),
  ('sementes-de-abobora-tostadas', 'Sementes de Abóbora Tostadas')
) AS r(slug, old_name)
WHERE g.slug = r.slug;

-- 2. Incrementa a revisão do catálogo
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
