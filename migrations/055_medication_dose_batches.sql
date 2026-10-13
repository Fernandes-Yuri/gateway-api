-- Fase 1: estrutura aditiva e identidade UTC compartilhada entre canais.
BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE medications ADD COLUMN IF NOT EXISTS schedule_revision integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS medication_dose_schedules (
 medication_id uuid NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
 local_time text NOT NULL CHECK (local_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
 dose_units integer CHECK (dose_units BETWEEN 1 AND 20),
 quantity_confirmed boolean NOT NULL DEFAULT false,
 active boolean NOT NULL DEFAULT true,
 PRIMARY KEY(medication_id,local_time),
 CHECK (NOT quantity_confirmed OR dose_units IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS medication_dose_occurrences (
 id uuid PRIMARY KEY,
 patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 medication_id uuid NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
 scheduled_at timestamptz NOT NULL,
 local_date date NOT NULL, local_time text NOT NULL, timezone text NOT NULL,
 log_id uuid REFERENCES medication_logs(id) ON DELETE SET NULL,
 historical_conflict boolean NOT NULL DEFAULT false,
 UNIQUE(patient_id,medication_id,scheduled_at)
);
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS occurrence_id uuid REFERENCES medication_dose_occurrences(id) ON DELETE SET NULL;
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS declared_taken_at timestamptz;
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS channel text;
ALTER TABLE medication_logs ADD COLUMN IF NOT EXISTS canonical_guard boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS medication_logs_one_canonical_dose ON medication_logs(occurrence_id)
 WHERE canonical_guard AND occurrence_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS medication_logs_occurrence ON medication_logs(occurrence_id);
CREATE TABLE IF NOT EXISTS medication_dose_migration_issues (
 log_id uuid PRIMARY KEY REFERENCES medication_logs(id) ON DELETE CASCADE,
 reason_code text NOT NULL, original_scheduled_for text
);
CREATE TABLE IF NOT EXISTS medication_dose_batches (
 id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
 patient_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 actor_id varchar(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 client_batch_id uuid NOT NULL, channel text NOT NULL CHECK(channel IN ('app','whatsapp')),
 group_id uuid NOT NULL, request_digest text NOT NULL, schedule_digest text NOT NULL,
 snapshot_digest text NOT NULL, snapshot jsonb NOT NULL,
 state text NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','confirmed','partially_confirmed','failed','cancelled','expired')),
 prepared_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '5 minutes',
 confirmation_id uuid, confirmation_digest text, result jsonb,
 UNIQUE(actor_id,client_batch_id)
);
CREATE TABLE IF NOT EXISTS medication_dose_batch_items (
 batch_id uuid NOT NULL REFERENCES medication_dose_batches(id) ON DELETE CASCADE,
 occurrence_id uuid NOT NULL REFERENCES medication_dose_occurrences(id) ON DELETE CASCADE,
 snapshot jsonb NOT NULL, selected boolean, result jsonb,
 PRIMARY KEY(batch_id,occurrence_id)
);

-- Todos os escritores legados passam por esta sincronização no banco.
CREATE OR REPLACE FUNCTION medication_schedule_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.name,NEW.dosage_mg,NEW.schedule_time,NEW.schedule_times,NEW.meal_context,
        NEW.pharmaceutical_form,NEW.frequency_interval_hours,NEW.treatment_duration_days)
 IS DISTINCT FROM ROW(OLD.name,OLD.dosage_mg,OLD.schedule_time,OLD.schedule_times,OLD.meal_context,
        OLD.pharmaceutical_form,OLD.frequency_interval_hours,OLD.treatment_duration_days) THEN
   NEW.schedule_revision := OLD.schedule_revision+1;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS medication_schedule_revision ON medications;
CREATE TRIGGER medication_schedule_revision BEFORE UPDATE ON medications FOR EACH ROW EXECUTE FUNCTION medication_schedule_revision();
CREATE OR REPLACE FUNCTION medication_sync_dose_slots() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE slot text;
BEGIN
 UPDATE medication_dose_schedules SET active=false WHERE medication_id=NEW.id
 AND local_time <> ALL(string_to_array(COALESCE(NULLIF(NEW.schedule_times,''),NEW.schedule_time),','));
 FOREACH slot IN ARRAY string_to_array(COALESCE(NULLIF(NEW.schedule_times,''),NEW.schedule_time),',') LOOP
  slot := btrim(slot);
  IF slot ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$' THEN
   INSERT INTO medication_dose_schedules(medication_id,local_time) VALUES(NEW.id,slot)
   ON CONFLICT(medication_id,local_time) DO UPDATE SET
    dose_units=CASE WHEN medication_dose_schedules.active THEN medication_dose_schedules.dose_units ELSE NULL END,
    quantity_confirmed=CASE WHEN medication_dose_schedules.active THEN medication_dose_schedules.quantity_confirmed ELSE false END,
    active=true;
  END IF;
 END LOOP;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS medication_sync_dose_slots ON medications;
CREATE TRIGGER medication_sync_dose_slots AFTER INSERT OR UPDATE OF schedule_time,schedule_times ON medications
 FOR EACH ROW EXECUTE FUNCTION medication_sync_dose_slots();
INSERT INTO medication_dose_schedules(medication_id,local_time)
 SELECT m.id,btrim(s) FROM medications m,
 LATERAL unnest(string_to_array(COALESCE(NULLIF(m.schedule_times,''),m.schedule_time),',')) s
 WHERE btrim(s) ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
 ON CONFLICT DO NOTHING;

-- Não se presume fuso histórico para textos sem offset. Preservar para revisão.
DO $$ DECLARE r record; instant timestamptz; oid uuid; zone text; BEGIN
 FOR r IN SELECT l.*,p.timezone FROM medication_logs l JOIN profiles p ON p.id=l.user_id
 WHERE l.occurrence_id IS NULL ORDER BY l.taken_at,l.id LOOP
  IF r.scheduled_for IS NULL OR r.scheduled_for !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
   INSERT INTO medication_dose_migration_issues VALUES(r.id,'HISTORICAL_TIMEZONE_REQUIRED',r.scheduled_for) ON CONFLICT DO NOTHING;
   CONTINUE;
  END IF;
  BEGIN instant:=r.scheduled_for::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
   INSERT INTO medication_dose_migration_issues VALUES(r.id,'HISTORICAL_TIME_INVALID',r.scheduled_for) ON CONFLICT DO NOTHING;
   CONTINUE;
  END;
  SELECT COALESCE((SELECT name FROM pg_timezone_names WHERE name=r.timezone),'America/Sao_Paulo') INTO zone;
  oid:=uuid_generate_v5(uuid_ns_url(),r.user_id||'/'||r.medication_id::text||'/'||to_char(instant AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
  INSERT INTO medication_dose_occurrences(id,patient_id,medication_id,scheduled_at,local_date,local_time,timezone,log_id)
  VALUES(oid,r.user_id,r.medication_id,instant,(instant AT TIME ZONE zone)::date,to_char(instant AT TIME ZONE zone,'HH24:MI'),zone,r.id)
  ON CONFLICT(patient_id,medication_id,scheduled_at) DO UPDATE SET historical_conflict=true;
  UPDATE medication_logs SET occurrence_id=oid WHERE id=r.id;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION medication_batch_terminal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state <> 'prepared' AND NEW IS DISTINCT FROM OLD THEN
  RAISE EXCEPTION 'Terminal medication batch is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS medication_batch_terminal_guard ON medication_dose_batches;
CREATE TRIGGER medication_batch_terminal_guard BEFORE UPDATE ON medication_dose_batches
 FOR EACH ROW EXECUTE FUNCTION medication_batch_terminal_guard();
INSERT INTO schema_migrations(version) VALUES('055_medication_dose_batches') ON CONFLICT DO NOTHING;
COMMIT;
