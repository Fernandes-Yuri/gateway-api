-- Retirada autorizada do módulo de alimentação em 26/10/2026.
-- Backup RDS: braga-before-nutrition-retirement-20261026-190311.
-- Inventário revisado: 20 tabelas, 1823 linhas e nenhuma FK externa ao módulo.
-- RESTRICT implícito: dependências não previstas abortam a transação inteira.
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
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
