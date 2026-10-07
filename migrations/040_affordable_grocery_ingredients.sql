-- =============================================================
-- MIGRACAO 040: Ingredientes de alimentacao acessivel e referencias de preco
-- Sincroniza o catalogo canonico com o app Android (PR #54), adicionando
-- os novos ingredientes com alternativas de custo acessivel e cotacoes base.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

-- 1. Insere os novos slugs de ingredientes na tabela canonica
INSERT INTO grocery_ingredients (slug, name, unit) VALUES
  ('oleo-de-soja', 'Óleo de Soja', 'L'),
  ('ovos', 'Ovos', 'un'),
  ('pao-integral', 'Pão integral', 'kg'),
  ('salsinha', 'Salsinha', 'kg'),
  ('tapioca', 'Tapioca', 'kg')
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, unit = EXCLUDED.unit;

-- 2. Insere referencias de preco inicial no cache para os novos alimentos e preparos
INSERT INTO grocery_price_cache (food_name, search_term, price_avg, price_min, price_max, source, price_unit, updated_at) VALUES
  ('Óleo de Soja', 'oleo de soja 900ml', 8.20, 7.29, 9.50, 'estimated', 'L', NOW()),
  ('Ovos', 'ovos brancos duzia', 0.85, 0.70, 1.10, 'estimated', 'un', NOW()),
  ('Pão integral', 'pao integral forma 500g', 16.00, 12.00, 22.00, 'estimated', 'kg', NOW()),
  ('Salsinha', 'salsinha fresca maco', 15.00, 10.00, 20.00, 'estimated', 'kg', NOW()),
  ('Tapioca', 'goma tapioca 1kg', 9.50, 7.00, 12.00, 'estimated', 'kg', NOW()),
  ('Ovo Mexido com Óleo de Soja', 'ovos brancos duzia', 0.85, 0.70, 1.10, 'estimated', 'un', NOW())
ON CONFLICT (food_name) DO UPDATE SET
  price_avg = EXCLUDED.price_avg,
  price_min = EXCLUDED.price_min,
  price_max = EXCLUDED.price_max,
  source = EXCLUDED.source,
  price_unit = EXCLUDED.price_unit,
  updated_at = EXCLUDED.updated_at;

INSERT INTO schema_migrations(version) VALUES ('040_affordable_grocery_ingredients') ON CONFLICT DO NOTHING;
COMMIT;
