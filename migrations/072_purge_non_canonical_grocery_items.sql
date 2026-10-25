-- Migração 072: Expurgo de itens não comerciais e ingredientes fora do catálogo canônico de supermercados.
-- Preserva integridade referencial e históricos de preços/contribuições via inativação lógica.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- Tabela de auditoria para rastrear o expurgo da revisão 072 e suportar rollback determinístico
CREATE TABLE IF NOT EXISTS grocery_curation_072 (
  entity TEXT NOT NULL CHECK(entity IN ('ingredient', 'food', 'card')),
  id TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (entity, id)
);

CREATE TEMP TABLE purged_slugs_072 (slug TEXT PRIMARY KEY) ON COMMIT DROP;

INSERT INTO purged_slugs_072 (slug) VALUES
  ('abacate-hass-avocado'),
  ('abobora-menina-brasileira'),
  ('acerola-fresca'),
  ('alcachofra-coracao'),
  ('alface-lisa'),
  ('alface-roxa'),
  ('almeirao'),
  ('amaranto-em-flocos'),
  ('amaranto-em-graos'),
  ('amora-preta-silvestre'),
  ('arroz-negro-integral'),
  ('arroz-vermelho-integral'),
  ('aspargos-frescos'),
  ('atum-fresco'),
  ('aveia-em-flocos-grossos'),
  ('bacalhau-dessalgado'),
  ('batata-doce-branca'),
  ('bertalha'),
  ('brocolis-ramoso'),
  ('broto-de-alfafa-fresco'),
  ('broto-de-bambu'),
  ('broto-de-feijao-moyashi'),
  ('cacao-em-posta'),
  ('caju-fresco'),
  ('canela-em-pau-para-infusao'),
  ('canela-em-po-do-ceilao'),
  ('caqui-fuyu-firme'),
  ('cara'),
  ('carambola-doce'),
  ('cardamomo-em-graos-triturado'),
  ('cevadinha-em-graos'),
  ('cha-de-alecrim-fresco'),
  ('cha-de-cavalinha'),
  ('cha-de-dente-de-leao-raiz'),
  ('cha-de-hibisco-flor-calice'),
  ('cha-de-hortela-fresca'),
  ('cha-gengibre-limao-saches'),
  ('chocolate-amargo-85-cacau'),
  ('claras-de-ovo'),
  ('couve-de-bruxelas'),
  ('cravo-da-india-em-grao'),
  ('erva-doce-em-sementes-funcho-seco'),
  ('ervilha-fresca-em-vagem-torta'),
  ('ervilha-seca-partida'),
  ('farelo-de-trigo-fibra-de-trigo'),
  ('farinha-de-banana-verde'),
  ('farinha-de-maracuja'),
  ('fava'),
  ('feijao-branco'),
  ('feijao-fradinho'),
  ('feijao-vermelho'),
  ('figo-roxo-fresco'),
  ('file-de-badejo'),
  ('file-mignon-bovino'),
  ('gergelim-branco-integral'),
  ('gergelim-preto'),
  ('germen-de-trigo-tostado'),
  ('goiaba-branca-com-casca'),
  ('granola-artesanal-sem-acucar'),
  ('jabuticaba-fresca'),
  ('jilo'),
  ('kefir-de-leite-artesanal'),
  ('lentilha-rosa-laranja'),
  ('lima-da-persia-fresca'),
  ('linhaca-marrom-moida'),
  ('lula'),
  ('macadamia-crua'),
  ('maracuja-azedo-polpa'),
  ('massa-de-arroz-integral'),
  ('massa-de-grao-de-bico'),
  ('melissa-fresca'),
  ('mexerica-ponkan'),
  ('milheto-em-graos'),
  ('milho-verde-na-espiga'),
  ('mirtilo-blueberry-fresco'),
  ('mostarda-em-graos-artesanal-sem-acucar'),
  ('musculo-bovino-moido'),
  ('nabo-branco'),
  ('noz-moscada-ralada-na-hora'),
  ('oleo-de-abacate-extravirgem'),
  ('paleta-bovina'),
  ('palmito-pupunha-fresco'),
  ('pescada-branca'),
  ('pimenta-do-reino-moida-na-hora'),
  ('pitanga-vermelha'),
  ('polenta-de-milho'),
  ('psyllium-puro-em-po'),
  ('queijo-meia-cura-em-dose-controlada'),
  ('quinoa-em-flocos'),
  ('rabanete-fatiado-cru'),
  ('rucula-selvagem'),
  ('sementes-de-abobora-tostadas'),
  ('sementes-de-girassol-descascadas'),
  ('soja-verde-em-graos-edamame'),
  ('taioba'),
  ('tofu-firme-organico'),
  ('tomilho-seco-fresco'),
  ('trigo-em-grao-integral'),
  ('vinagre-de-maca-organico-nao-filtrado');

-- 1. Inativação em grocery_ingredients
INSERT INTO grocery_curation_072 (entity, id, reason)
SELECT 'ingredient', i.slug, 'expurgo 072: ingrediente fora do catalogo canonico de supermercados'
FROM grocery_ingredients i
JOIN purged_slugs_072 p ON p.slug = i.slug
WHERE i.active
ON CONFLICT (entity, id) DO NOTHING;

UPDATE grocery_ingredients
SET active = FALSE
WHERE slug IN (SELECT id FROM grocery_curation_072 WHERE entity = 'ingredient');

-- 2. Inativação em grocery_retail_cards vinculados
INSERT INTO grocery_curation_072 (entity, id, reason)
SELECT 'card', c.id, 'expurgo 072: cartao vinculado a ingrediente purgado'
FROM grocery_retail_cards c
JOIN purged_slugs_072 p ON p.slug = c.linked_ingredient_slug
WHERE c.active
ON CONFLICT (entity, id) DO NOTHING;

UPDATE grocery_retail_cards
SET active = FALSE
WHERE id IN (SELECT id FROM grocery_curation_072 WHERE entity = 'card');

-- 3. Inativação em food_catalog com componentes de compra purgados
INSERT INTO grocery_curation_072 (entity, id, reason)
SELECT 'food', f.remote_id, 'expurgo 072: alimento associado a ingrediente purgado'
FROM food_catalog f
WHERE f.active
  AND (
    EXISTS (
      SELECT 1
      FROM jsonb_array_elements_text(f.details->'shoppingComponents') s
      JOIN purged_slugs_072 p ON p.slug = s
    )
    OR EXISTS (
      SELECT 1
      FROM grocery_ingredients gi
      JOIN purged_slugs_072 p ON p.slug = gi.slug
      WHERE gi.details->'food_ids' ? f.remote_id
    )
  )
ON CONFLICT (entity, id) DO NOTHING;

UPDATE food_catalog
SET active = FALSE
WHERE remote_id IN (SELECT id FROM grocery_curation_072 WHERE entity = 'food');

-- 4. Incremento da revisão do catálogo para forçar sincronização do app cliente
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

-- 5. Registro em schema_migrations (gerenciado transacionalmente pelo runner)
INSERT INTO schema_migrations (version) VALUES ('072_purge_non_canonical_grocery_items') ON CONFLICT (version) DO NOTHING;

COMMIT;
