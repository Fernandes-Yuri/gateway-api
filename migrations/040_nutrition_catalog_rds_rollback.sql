-- Compensação de dados: aplicar apenas em nova migration pelo pipeline.
-- Não apaga alimentos ou contribuições criados após a atualização.
BEGIN;
SET LOCAL lock_timeout = '10s';
UPDATE food_catalog SET active=FALSE WHERE remote_id IN ('catalog_arroz_branco_tipo_1','catalog_pts');
UPDATE food_catalog SET active=TRUE WHERE remote_id IN ('food_130','food_135');
UPDATE grocery_ingredients SET active=FALSE WHERE slug IN ('arroz-branco-tipo-1','proteina-texturizada-de-soja');
UPDATE grocery_ingredients SET active=TRUE WHERE slug IN ('arroz-negro-integral','soja-verde-em-graos-edamame');
UPDATE nutrition_catalog_settings SET revision=revision+1,
 policy='{"required_groups":[["food_132","food_133"],["food_121","food_122"]]}'::jsonb WHERE id=1;
COMMIT;
