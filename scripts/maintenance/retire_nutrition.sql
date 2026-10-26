-- Etapa destrutiva MANUAL: revisar inventário e backup antes de executar.
-- Fora de migrations/: deploy normal não apaga dados de produção.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
DO $$ BEGIN
 IF current_setting('braga.allow_nutrition_purge', true) IS DISTINCT FROM 'on' THEN
  RAISE EXCEPTION 'Exclusão não habilitada: revisar backup e definir braga.allow_nutrition_purge=on na sessão';
 END IF;
END $$;
DROP TABLE IF EXISTS public.grocery_publication_heads;
DROP TABLE IF EXISTS public.grocery_publications;
DROP TABLE IF EXISTS public.grocery_publication_state;
DROP TABLE IF EXISTS public.grocery_editorial_pack_items;
DROP TABLE IF EXISTS public.grocery_editorial_packs;
DROP TABLE IF EXISTS public.grocery_edition_quotes;
DROP TABLE IF EXISTS public.grocery_editions;
DROP TABLE IF EXISTS public.grocery_market_prices;
DROP TABLE IF EXISTS public.grocery_retail_cards;
DROP TABLE IF EXISTS public.grocery_markets;
DROP TABLE IF EXISTS public.grocery_price_contributions;
DROP TABLE IF EXISTS public.grocery_price_cache_history;
DROP TABLE IF EXISTS public.grocery_price_cache;
DROP TABLE IF EXISTS public.grocery_ingredients;
DROP TABLE IF EXISTS public.nutrition_catalog_settings;
DROP TABLE IF EXISTS public.food_semantic_metadata_backup_072;
DROP TABLE IF EXISTS public.grocery_curation_072;
DROP TABLE IF EXISTS public.grocery_curation_065;
DROP TABLE IF EXISTS public.user_consumptions;
DROP TABLE IF EXISTS public.food_catalog;
DROP FUNCTION IF EXISTS public.grocery_publication_immutable();
DROP FUNCTION IF EXISTS public.grocery_physical_metadata_valid(jsonb);
DROP FUNCTION IF EXISTS public.grocery_component_proportions_valid(jsonb);
ALTER TABLE public.profiles DROP COLUMN IF EXISTS daily_calorie_target;
COMMIT;
