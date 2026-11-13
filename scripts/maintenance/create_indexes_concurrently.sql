-- =============================================================
-- Braga Saude - Criacao Concorrente de Indices em Producao (Zero Downtime)
--
-- ATENCAO: Este script DEVE ser executado fora de blocos de transacao.
-- O comando CREATE INDEX CONCURRENTLY constroi indices em background sem
-- adquirir lock exclusivo de escrita (INSERT/UPDATE/DELETE continuam operando).
--
-- Uso via terminal / AWS SSM:
--   psql "$DATABASE_URL" -f scripts/maintenance/create_indexes_concurrently.sql
-- =============================================================

\echo 'Iniciando criacao concorrente de indices (zero downtime)...'

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vital_signs_active_history
    ON vital_signs (user_id, measured_at DESC, id DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_medication_logs_medication_taken
    ON medication_logs (medication_id, taken_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_device_push_tokens_user_active
    ON device_push_tokens (user_id)
    WHERE is_active = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_social_posts_user_created
    ON social_posts (user_id, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_family_bindings_active_pair
    ON family_bindings (patient_id, caregiver_id)
    WHERE status = 'ACTIVE';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_consultations_caregiver_date
    ON consultations (caregiver_id, scheduled_date);

\echo 'Indices concorrentes criados com sucesso.'
