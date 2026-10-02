-- Preserva o histórico clínico de outros pacientes ao excluir um cuidador.
DO $$
DECLARE item record; fk record;
BEGIN
  FOR item IN SELECT * FROM (VALUES
    ('prescriptions','confirmed_by'), ('care_audit_trail','actor_id'),
    ('medication_logs','actor_id'), ('medication_stock_adjustments','actor_id'),
    ('symptoms_diary','reported_by')) AS fields(table_name,column_name)
  LOOP
    EXECUTE format('ALTER TABLE %I ALTER COLUMN %I DROP NOT NULL', item.table_name,item.column_name);
    FOR fk IN SELECT c.conname FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
      WHERE c.conrelid=to_regclass(item.table_name) AND c.contype='f' AND a.attname=item.column_name
    LOOP
      EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',item.table_name,fk.conname);
    END LOOP;
    EXECUTE format('ALTER TABLE %I ADD FOREIGN KEY (%I) REFERENCES profiles(id) ON DELETE SET NULL',item.table_name,item.column_name);
  END LOOP;
END $$;
ALTER TABLE user_deletion_logs ADD COLUMN IF NOT EXISTS admin_email VARCHAR(254);
CREATE TABLE IF NOT EXISTS account_storage_cleanup (
    user_id VARCHAR(128) PRIMARY KEY,
    prescription_filenames JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
