BEGIN;
SET LOCAL lock_timeout = '10s';
UPDATE food_catalog c SET details = (c.details - 'semanticMetadata' - 'maxDailyGrams' - 'functionalTags')
    || CASE WHEN b.metadata IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('semanticMetadata', b.metadata) END
    || CASE WHEN b.daily_cap IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('maxDailyGrams', b.daily_cap) END
    || CASE WHEN b.tags IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('functionalTags', b.tags) END
FROM food_semantic_metadata_backup_072 b WHERE c.remote_id = b.remote_id;
DROP TABLE food_semantic_metadata_backup_072;
UPDATE nutrition_catalog_settings SET revision = revision + 1 WHERE id = 1;
COMMIT;
