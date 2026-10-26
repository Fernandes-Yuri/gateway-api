-- Somente leitura: estrutura e contagens, sem dados pessoais.
SELECT current_database() AS database_name;
SELECT tc.table_name, kcu.column_name, ccu.table_name AS referenced_table
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu USING (constraint_catalog, constraint_schema, constraint_name)
JOIN information_schema.constraint_column_usage ccu USING (constraint_catalog, constraint_schema, constraint_name)
WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
ORDER BY tc.table_name, kcu.column_name;
DO $$
DECLARE target text; total bigint;
BEGIN
 FOREACH target IN ARRAY ARRAY[
  'grocery_publication_heads',
  'grocery_publications',
  'grocery_publication_state',
  'grocery_editorial_pack_items',
  'grocery_editorial_packs',
  'grocery_edition_quotes',
  'grocery_editions',
  'grocery_market_prices',
  'grocery_retail_cards',
  'grocery_markets',
  'grocery_price_contributions',
  'grocery_price_cache_history',
  'grocery_price_cache',
  'grocery_ingredients',
  'nutrition_catalog_settings',
  'food_semantic_metadata_backup_072',
  'grocery_curation_072',
  'grocery_curation_065',
  'user_consumptions',
  'food_catalog'
 ] LOOP
  IF to_regclass(format('public.%I', target)) IS NOT NULL THEN
   EXECUTE format('SELECT count(*) FROM public.%I', target) INTO total;
   RAISE NOTICE 'table=% rows=%', target, total;
  END IF;
 END LOOP;
END $$;
