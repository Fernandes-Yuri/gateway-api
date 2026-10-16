BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE grocery_editorial_packs ADD COLUMN planning_days INTEGER NOT NULL DEFAULT 7 CHECK (planning_days IN (7,14,30));
COMMIT;
