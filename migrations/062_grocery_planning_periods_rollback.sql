BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_editorial_packs DROP COLUMN planning_days;
COMMIT;
