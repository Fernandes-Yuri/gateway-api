-- Rollback da migração 043: desativa novos itens de café/lanche e reativa variantes
BEGIN;
SET LOCAL lock_timeout = '10s';

UPDATE food_catalog
SET active = false
WHERE remote_id BETWEEN 'food_221' AND 'food_240';

UPDATE food_catalog
SET active = true
WHERE remote_id IN (
  'food_006', 'food_023', 'food_063', 'food_064', 'food_065',
  'food_066', 'food_068', 'food_069', 'food_155', 'food_163',
  'food_176', 'food_193', 'food_196'
);

UPDATE grocery_ingredients
SET active = false
WHERE slug IN (
  'pao-frances', 'pao-de-forma-integral', 'goma-de-tapioca', 'torrada-integral',
  'pao-de-queijo', 'milho-para-pipoca', 'biscoito-de-polvilho', 'requeijao-cremoso',
  'manteiga-com-sal', 'queijo-mussarela', 'atum-ralado-ao-natural', 'po-de-cafe-tradicional'
);

UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
