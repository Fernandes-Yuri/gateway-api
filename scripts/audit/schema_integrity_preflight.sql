-- =============================================================
-- Braga Saude - Script de Auditoria Pre-flight de Integridade
-- Execucao em Modo Somente Leitura (zero mutacao de dados).
-- Uso: psql "$DATABASE_URL" -f scripts/audit/schema_integrity_preflight.sql
-- =============================================================

SELECT current_database() AS database_name, now() AS audit_timestamp;

-- 1. Contagem de Linhas em Tabelas Zumbis / Mortas
SELECT 'zombie_biometries' AS check_name, count(*) AS total_rows FROM biometries
UNION ALL
SELECT 'zombie_pending_confirmations' AS check_name, count(*) AS total_rows FROM pending_confirmations
UNION ALL
SELECT 'zombie_confirmation_logs' AS check_name, count(*) AS total_rows FROM confirmation_logs;

-- 2. Verificacao de Sinais Vitais e Biometria fora de faixa fisiologica
SELECT 'vital_signs_out_of_range' AS check_name, count(*) AS broken_rows
FROM vital_signs
WHERE (systolic_pressure IS NOT NULL AND (systolic_pressure < 40 OR systolic_pressure > 300))
   OR (diastolic_pressure IS NOT NULL AND (diastolic_pressure < 20 OR diastolic_pressure > 200))
   OR (heart_rate IS NOT NULL AND (heart_rate < 20 OR heart_rate > 250))
   OR (oxygen_saturation IS NOT NULL AND (oxygen_saturation < 30.0 OR oxygen_saturation > 100.0))
   OR (glucose_level IS NOT NULL AND (glucose_level < 10 OR glucose_level > 1000))
   OR (hydration_ml IS NOT NULL AND (hydration_ml <= 0 OR hydration_ml > 20000));

SELECT 'vital_signs_all_null' AS check_name, count(*) AS broken_rows
FROM vital_signs
WHERE deleted_at IS NULL
  AND num_nonnulls(systolic_pressure, diastolic_pressure, heart_rate, oxygen_saturation, glucose_level, hydration_ml) = 0;

SELECT 'biometrics_out_of_range' AS check_name, count(*) AS broken_rows
FROM biometrics
WHERE weight_kg <= 0 OR weight_kg > 500
   OR (height_m IS NOT NULL AND (height_m < 0.30 OR height_m > 2.50))
   OR (imc IS NOT NULL AND (imc < 5.0 OR imc > 150.0));

-- 3. Verificacao de Registros Orfaos (Potenciais Violacoes de FK)
SELECT 'orphan_device_push_tokens' AS check_name, count(*) AS broken_rows
FROM device_push_tokens d
LEFT JOIN profiles p ON p.id = d.user_id
WHERE p.id IS NULL;

SELECT 'orphan_vital_sign_changes' AS check_name, count(*) AS broken_rows
FROM vital_sign_changes c
LEFT JOIN vital_signs v ON v.id = c.vital_id
WHERE v.id IS NULL;

SELECT 'orphan_notification_sources' AS check_name, count(*) AS broken_rows
FROM notification_sources s
LEFT JOIN profiles p ON p.id = s.patient_id
WHERE p.id IS NULL;

SELECT 'orphan_feedbacks_user' AS check_name, count(*) AS broken_rows
FROM feedbacks f
LEFT JOIN profiles p ON p.id = f.user_id
WHERE f.user_id IS NOT NULL AND p.id IS NULL;

SELECT 'orphan_feedback_replies_user' AS check_name, count(*) AS broken_rows
FROM feedback_replies r
LEFT JOIN profiles p ON p.id = r.user_id
WHERE p.id IS NULL;

-- 4. Verificacao de Medicamentos, Sintomas e Consultas
SELECT 'medications_out_of_range' AS check_name, count(*) AS broken_rows
FROM medications
WHERE (dosage_mg IS NOT NULL AND (dosage_mg <= 0 OR dosage_mg > 100000))
   OR (frequency_interval_hours IS NOT NULL AND (frequency_interval_hours < 1 OR frequency_interval_hours > 48))
   OR (treatment_duration_days IS NOT NULL AND (treatment_duration_days < 1 OR treatment_duration_days > 365))
   OR (ean_barcode IS NOT NULL AND ean_barcode !~ '^[0-9]{13}$');

SELECT 'symptoms_diary_invalid' AS check_name, count(*) AS broken_rows
FROM symptoms_diary
WHERE (sleep_quality IS NOT NULL AND (sleep_quality < 1 OR sleep_quality > 5))
   OR (disposition IS NOT NULL AND (disposition < 1 OR disposition > 5))
   OR input_method NOT IN ('VOICE', 'TEXT')
   OR char_length(symptoms_text) < 1 OR char_length(symptoms_text) > 1000;

SELECT 'consultations_invalid_status' AS check_name, count(*) AS broken_rows
FROM consultations
WHERE status NOT IN ('SUGGESTED', 'ACCEPTED', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'COMPLETED');

-- 5. Verificacao de Familia, Perfis e Social
SELECT 'self_family_bindings' AS check_name, count(*) AS broken_rows
FROM family_bindings
WHERE patient_id = caregiver_id;

SELECT 'family_bindings_invalid_status' AS check_name, count(*) AS broken_rows
FROM family_bindings
WHERE status NOT IN ('PENDING', 'ACTIVE', 'REVOKED', 'EXPIRED', 'REJECTED');

SELECT 'social_posts_invalid_visibility' AS check_name, count(*) AS broken_rows
FROM social_posts
WHERE visibility NOT IN ('PUBLIC', 'FAMILY', 'PRIVATE');

SELECT 'profiles_invalid_values' AS check_name, count(*) AS broken_rows
FROM profiles
WHERE (user_role IS NOT NULL AND user_role NOT IN ('PATIENT', 'CAREGIVER'))
   OR (sleep_start_time IS NOT NULL AND sleep_start_time !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')
   OR (sleep_end_time IS NOT NULL AND sleep_end_time !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')
   OR (hydration_target_ml IS NOT NULL AND (hydration_target_ml < 500 OR hydration_target_ml > 10000))
   OR (step_goal IS NOT NULL AND (step_goal < 1000 OR step_goal > 100000));
