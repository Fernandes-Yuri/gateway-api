-- Migração 042: Conversões físicas auditadas, agrupadores canônicos e fatores de cocção/in natura.
-- Preserva histórico, dados existentes e revisão do catálogo.
BEGIN;

-- 1. Densidades de referência para ingredientes líquidos comercializados em Litros (kg/L ou g/ml)
-- Baseada em literatura físico-química e TACO/tabelas de composição.
UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
    'density_g_per_ml', 1.02,
    'physical_metadata_source', 'referência físico-química: água de coco natural fluida (~1.02 g/ml)'
)
WHERE slug = 'agua-de-coco-natural' AND (details->>'density_g_per_ml') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
    'density_g_per_ml', 1.01,
    'physical_metadata_source', 'referência físico-química: bebida vegetal de amêndoas fluida (~1.01 g/ml)'
)
WHERE slug = 'bebida-vegetal-de-amendoas-sem-acucar' AND (details->>'density_g_per_ml') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
    'density_g_per_ml', 1.03,
    'physical_metadata_source', 'referência físico-química: bebida vegetal de aveia fluida (~1.03 g/ml)'
)
WHERE slug = 'bebida-vegetal-de-aveia-sem-acucar' AND (details->>'density_g_per_ml') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
    'density_g_per_ml', 1.03,
    'physical_metadata_source', 'referência físico-química: kefir de leite artesanal fluido (~1.03 g/ml)'
)
WHERE slug = 'kefir-de-leite-artesanal' AND (details->>'density_g_per_ml') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
    'density_g_per_ml', 1.01,
    'physical_metadata_source', 'referência físico-química: vinagre de maçã diluído a 4-5% (~1.01 g/ml)'
)
WHERE slug = 'vinagre-de-maca-organico-nao-filtrado' AND (details->>'density_g_per_ml') IS NULL;

-- 2. Agrupadores canônicos para orientação de variedade real na seleção semanal
-- Previne concentração de múltiplas variantes do mesmo ingrediente na mesma semana
UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'aveia')
WHERE slug IN ('farelo-de-aveia', 'aveia-em-flocos-finos', 'aveia-em-flocos-grossos', 'farinha-de-aveia-fina', 'farinha-de-aveia-integral', 'bebida-vegetal-de-aveia-sem-acucar')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'linhaca')
WHERE slug IN ('linhaca-marrom-moida', 'linhaca-dourada-moida')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'banana')
WHERE slug IN ('banana-prata', 'banana-maca')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'maca')
WHERE slug IN ('maca-gala-com-casca', 'maca-fuji-com-casca')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'mamao')
WHERE slug IN ('mamao-papaia', 'mamao-formosa')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'laranja')
WHERE slug IN ('laranja-pera-com-bagaco', 'laranja-lima')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'amendoim')
WHERE slug IN ('amendoim-torrado-sem-sal', 'amendoim-torrado-com-pele', 'pasta-de-amendoim-integral-100')
  AND (details->>'canonical_group_slug') IS NULL;

UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('canonical_group_slug', 'feijao')
WHERE slug IN ('feijao-carioca', 'feijao-preto', 'feijao-fradinho', 'feijao-branco', 'feijao-vermelho')
  AND (details->>'canonical_group_slug') IS NULL;

-- 3. Fatores de compra (purchaseFactors) para alimentos preparados clássicos e in natura
-- Feijões: rendimento médio de cocção ~2.2x (fator seco/cozido = 0.45)
UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('feijao-carioca', 0.45))
WHERE remote_id = 'food_121' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('feijao-preto', 0.45))
WHERE remote_id = 'food_122' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('feijao-fradinho', 0.45))
WHERE remote_id = 'food_123' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

-- Macarrão integral cozido: rendimento de hidratação ~2.2x (fator seco/cozido = 0.45)
UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('macarrao-integral', 0.45))
WHERE remote_id = 'food_143' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

-- Ovo cozido: ovo in natura cozido com casca retém a massa comestível (fator 1.0 justificado)
UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('ovo-caipira', 1.0))
WHERE remote_id = 'food_146' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

-- Alimentos consumidos in natura na mesma forma de compra (fator 1.0 justificado)
UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('farelo-de-aveia', 1.0))
WHERE remote_id = 'food_001' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('aveia-em-flocos-finos', 1.0))
WHERE remote_id = 'food_004' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('aveia-em-flocos-grossos', 1.0))
WHERE remote_id = 'food_005' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('linhaca-marrom-moida', 1.0))
WHERE remote_id = 'food_006' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('banana-prata', 1.0))
WHERE remote_id = 'food_026' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('maca-gala-com-casca', 1.0))
WHERE remote_id = 'food_028' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

UPDATE food_catalog
SET details = jsonb_set(details, '{purchaseFactors}', jsonb_build_object('mamao-papaia', 1.0))
WHERE remote_id = 'food_031' AND (details->'purchaseFactors' = '{}'::jsonb OR details->'purchaseFactors' IS NULL);

-- Não inventar proporções de receitas compostas (ex: food_217, food_220) para que o sistema registre
-- a limitação estruturada de dados pendentes sem mascarar completude.

-- Atualização da revisão do catálogo
UPDATE nutrition_catalog_settings SET revision = greatest(revision, 4) WHERE id = 1;
COMMIT;
