-- =============================================================
-- MIGRACAO 089: Indices direcionados para consultas de alta frequencia
-- e blindagem de Foreign Keys criticas.
--
-- Executado dentro de transacao pelo runner canônico (migrate.py).
-- Protegido com lock_timeout de 5s para evitar bloqueios em tabelas ativas.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. Sinais vitais ativos (elimina Sort e Filter de deleted_at em GET /api/sync/vitals)
CREATE INDEX IF NOT EXISTS idx_vital_signs_active_history
    ON vital_signs (user_id, measured_at DESC, id DESC)
    WHERE deleted_at IS NULL;

-- 2. Logs de tomada por medicamento (FK medications + timeline de remédios)
CREATE INDEX IF NOT EXISTS idx_medication_logs_medication_taken
    ON medication_logs (medication_id, taken_at DESC);

-- 3. Tokens push de usuarios ativos (core/devices.py)
CREATE INDEX IF NOT EXISTS idx_device_push_tokens_user_active
    ON device_push_tokens (user_id)
    WHERE is_active = true;

-- 4. Posts sociais por usuario (mural do autor + protecao FK profiles)
CREATE INDEX IF NOT EXISTS idx_social_posts_user_created
    ON social_posts (user_id, created_at DESC);

-- 5. Par de vinculo ativo para autorizacao Zero-Trust (Care OS + Go Chat)
CREATE INDEX IF NOT EXISTS idx_family_bindings_active_pair
    ON family_bindings (patient_id, caregiver_id)
    WHERE status = 'ACTIVE';

-- 6. Consultas agendadas do cuidador (agenda do cuidador + protecao FK profiles)
CREATE INDEX IF NOT EXISTS idx_consultations_caregiver_date
    ON consultations (caregiver_id, scheduled_date);
