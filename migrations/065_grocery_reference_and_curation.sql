-- Curadoria conservadora: lista explícita de itens incomuns; nunca por tamanho do nome.
-- Somente inativa registros sem foto. Básicos fora da lista são preservados.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE grocery_curation_065 (
 entity TEXT NOT NULL CHECK(entity IN ('ingredient','food','card')),
 id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(entity,id)
);
CREATE TEMP TABLE curated_rare_slugs(slug TEXT PRIMARY KEY) ON COMMIT DROP;
INSERT INTO curated_rare_slugs VALUES
 ('oleo-de-abacate-extravirgem'),
 ('alcachofra-coracao'),
 ('amaranto-em-flocos'),
 ('amaranto-em-graos'),
 ('amora-preta-silvestre'),
 ('arroz-negro-integral'),
 ('arroz-vermelho-integral'),
 ('bertalha'),
 ('broto-de-alfafa-fresco'),
 ('broto-de-bambu'),
 ('cardamomo-em-graos-triturado'),
 ('cevadinha-em-graos'),
 ('cha-de-dente-de-leao-raiz'),
 ('farinha-de-maracuja'),
 ('germen-de-trigo-tostado'),
 ('kefir-de-leite-artesanal'),
 ('macadamia-crua'),
 ('massa-de-grao-de-bico'),
 ('milheto-em-graos'),
 ('mostarda-em-graos-artesanal-sem-acucar'),
 ('noz-moscada-ralada-na-hora'),
 ('palmito-pupunha-fresco'),
 ('pitanga-vermelha'),
 ('psyllium-puro-em-po'),
 ('rucula-selvagem'),
 ('soja-verde-em-graos-edamame'),
 ('taioba'),
 ('tofu-firme-organico'),
 ('trigo-em-grao-integral'),
 ('vinagre-de-maca-organico-nao-filtrado');
-- Fotos existentes no ingrediente, alimento associado ou embalagem protegem a família.
CREATE TEMP TABLE curated_ingredients ON COMMIT DROP AS
SELECT i.slug FROM grocery_ingredients i JOIN curated_rare_slugs r USING(slug)
WHERE i.active
 AND coalesce(nullif(trim(i.details->>'image_url'),''),nullif(trim(i.details->>'imageUrl'),''),'')=''
 AND NOT EXISTS(SELECT 1 FROM grocery_retail_cards c WHERE c.linked_ingredient_slug=i.slug AND trim(c.image_url)<>'')
 -- O seed legado contém IDs raros ligados a nomes comuns. Não retirar essas famílias.
 AND NOT EXISTS(SELECT 1 FROM food_catalog f WHERE f.active
   AND (i.details->'food_ids' ? f.remote_id OR f.details->'shoppingComponents' ? i.slug)
   AND regexp_replace(lower(f.name),'[^[:alnum:]]','','g') NOT LIKE
       regexp_replace(lower(i.name),'[^[:alnum:]]','','g') || '%')
 AND NOT EXISTS(SELECT 1 FROM food_catalog f WHERE
   (i.details->'food_ids' ? f.remote_id OR f.details->'shoppingComponents' ? i.slug)
   AND coalesce(nullif(trim(f.details->>'image_url'),''),nullif(trim(f.details->>'imageUrl'),''),'')<>'');
INSERT INTO grocery_curation_065 SELECT 'ingredient',slug,'incomum sem imagem' FROM curated_ingredients;
UPDATE grocery_ingredients SET active=FALSE WHERE slug IN(SELECT slug FROM curated_ingredients);
INSERT INTO grocery_curation_065
 SELECT 'card',c.id,'ingrediente incomum sem imagem' FROM grocery_retail_cards c
 JOIN curated_ingredients i ON i.slug=c.linked_ingredient_slug WHERE c.active AND trim(c.image_url)='';
UPDATE grocery_retail_cards SET active=FALSE WHERE id IN(SELECT id FROM grocery_curation_065 WHERE entity='card');
-- Pratos compostos sem foto saem da seleção; seus ingredientes básicos continuam disponíveis.
INSERT INTO grocery_curation_065
 SELECT 'food',f.remote_id,'composto ou ingrediente incomum sem imagem' FROM food_catalog f
 WHERE f.active
 AND coalesce(nullif(trim(f.details->>'image_url'),''),nullif(trim(f.details->>'imageUrl'),''),'')=''
 AND NOT EXISTS(SELECT 1 FROM grocery_retail_cards c WHERE trim(c.image_url)<>''
   AND f.details->'shoppingComponents' ? c.linked_ingredient_slug)
 AND (CASE WHEN jsonb_typeof(f.details->'shoppingComponents')='array'
       THEN jsonb_array_length(f.details->'shoppingComponents')>1 ELSE FALSE END
   OR EXISTS(SELECT 1 FROM curated_ingredients i WHERE f.details->'shoppingComponents' ? i.slug
       OR EXISTS(SELECT 1 FROM grocery_ingredients gi WHERE gi.slug=i.slug AND gi.details->'food_ids' ? f.remote_id)));
UPDATE food_catalog SET active=FALSE WHERE remote_id IN(SELECT id FROM grocery_curation_065 WHERE entity='food');
-- Aproveita apenas pesquisas comuns confirmadas; promoções condicionadas não viram referência.
INSERT INTO grocery_market_prices(card_id,market_id,price,product_store_url,last_updated)
 SELECT DISTINCT ON(card_id,market_id) card_id,market_id,price,product_store_url,researched_at
 FROM grocery_edition_quotes WHERE confirmed AND NOT membership_required AND minimum_quantity=1 AND trim(conditions)=''
 ORDER BY card_id,market_id,researched_at DESC,edition_id
ON CONFLICT(card_id,market_id) DO UPDATE SET price=EXCLUDED.price,
 product_store_url=EXCLUDED.product_store_url,last_updated=EXCLUDED.last_updated
 WHERE EXCLUDED.last_updated>grocery_market_prices.last_updated;
UPDATE nutrition_catalog_settings SET revision=revision+1 WHERE id=1;
COMMIT;
