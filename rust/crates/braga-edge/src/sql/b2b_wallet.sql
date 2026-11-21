-- $1 program, $2 optional page IDs, $3 enabled categories, $4 reminders,
-- $5 appointments, $6 request time, $7 direct chat enabled (apenas o SELECT
-- global em b2b_wallet_global.sql usa $7; a pagina de participantes nao).
-- Dias de calendario America/Sao_Paulo (mesma base do detalhe do participante),
-- hoje incluido.
-- Only program interactions; never reads personal health records or chat buffers.
WITH members AS MATERIALIZED (
    SELECT e.patient_user_id, e.consented_at, e.shared_categories
    FROM program_enrollments e
    WHERE e.program_id = $1 AND e.status = 'ACTIVE'
      AND ($2::text[] IS NULL OR e.patient_user_id = ANY($2))
), visible_reminders AS MATERIALIZED (
    SELECT e.patient_user_id, r.id, r.expires_at, rc.acknowledged_at,
           rc.acknowledged_by_uid
    FROM members e
    JOIN program_reminders r ON r.program_id = $1
      AND (r.target_patient_id IS NULL OR r.target_patient_id = e.patient_user_id)
      AND r.created_at >= e.consented_at AND r.created_at <= $6
      AND (r.target_category IS NULL OR (
          e.shared_categories @> jsonb_build_array(r.target_category)
          AND r.target_category = ANY($3::text[])))
    LEFT JOIN program_reminder_receipts rc ON rc.reminder_id = r.id
      AND rc.patient_user_id = e.patient_user_id
    WHERE $4::boolean AND (r.expires_at > $6 OR rc.acknowledged_at >=
        ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 13)::timestamp AT TIME ZONE 'America/Sao_Paulo'))
), appointments AS MATERIALIZED (
    SELECT a.* FROM program_appointments a
    JOIN members e ON e.patient_user_id = a.patient_user_id
    WHERE a.program_id = $1 AND a.created_at >= e.consented_at AND a.created_at <= $6
      AND $5::boolean
      AND (a.created_at >= ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 13)::timestamp AT TIME ZONE 'America/Sao_Paulo')
           OR (a.status = 'CONFIRMED' AND (a.scheduled_at >= $6 OR a.updated_at >=
               ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 13)::timestamp AT TIME ZONE 'America/Sao_Paulo'))))
), events AS (
    SELECT patient_user_id, (acknowledged_at AT TIME ZONE 'America/Sao_Paulo')::date AS day
    FROM visible_reminders
    WHERE acknowledged_by_uid = patient_user_id AND acknowledged_at <= $6
    UNION
    SELECT patient_user_id, (created_at AT TIME ZONE 'America/Sao_Paulo')::date FROM appointments
), activity AS (
    SELECT patient_user_id,
        array_agg(DISTINCT day ORDER BY day) FILTER (
            WHERE day >= ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6) AS days,
        bool_or(day < ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6) AS previous_week
    FROM events
    WHERE day BETWEEN ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 13
                  AND ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date
    GROUP BY patient_user_id
), reminders AS (
    SELECT patient_user_id,
        count(*) FILTER (WHERE acknowledged_at IS NULL AND expires_at > $6) AS pending,
        count(*) FILTER (WHERE acknowledged_at >=
            ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6)::timestamp AT TIME ZONE 'America/Sao_Paulo')
            AND acknowledged_at <= $6) AS read
    FROM visible_reminders GROUP BY patient_user_id
), reminders_total AS (
    -- Denominador de "% lido": lembretes entregues no recorte de 7 dias que
    -- existiram para o membro (criados no periodo) OU que foram lidos nele.
    -- Inclui lembrete expirado/nao lido (conta como enviado) e leitura atrasada
    -- de lembrete antigo (que o KPI `remindersRead` ja contabiliza), mantendo
    -- read <= total. Respeita consentimento/categoria/feature como a carteira.
    SELECT e.patient_user_id, count(*)::bigint AS total
    FROM members e
    JOIN program_reminders r ON r.program_id = $1
      AND (r.target_patient_id IS NULL OR r.target_patient_id = e.patient_user_id)
      AND r.created_at >= e.consented_at AND r.created_at <= $6
      AND (r.target_category IS NULL OR (
          e.shared_categories @> jsonb_build_array(r.target_category)
          AND r.target_category = ANY($3::text[])))
      AND $4::boolean
    WHERE r.created_at >=
          ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6)::timestamp AT TIME ZONE 'America/Sao_Paulo')
       OR EXISTS (
          SELECT 1 FROM program_reminder_receipts rc
           WHERE rc.reminder_id = r.id
             AND rc.patient_user_id = e.patient_user_id
             AND rc.acknowledged_at >=
                 ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6)::timestamp AT TIME ZONE 'America/Sao_Paulo'))
    GROUP BY e.patient_user_id
), bookings AS (
    SELECT patient_user_id,
        count(*) FILTER (WHERE status = 'CONFIRMED' AND scheduled_at >= $6
            AND scheduled_at < $6 + interval '7 days') AS upcoming,
        count(*) FILTER (WHERE status = 'CONFIRMED' AND updated_at >=
            ((($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 6)::timestamp AT TIME ZONE 'America/Sao_Paulo')
            AND updated_at <= $6) AS confirmed,
        count(*) FILTER (WHERE status = 'CONFIRMED' AND (updated_at AT TIME ZONE 'America/Sao_Paulo')::date
            BETWEEN ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 13
                AND ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date - 7) AS previous_confirmed
    FROM appointments GROUP BY patient_user_id
), metrics AS (
    SELECT e.patient_user_id,
        COALESCE(a.days::text[], ARRAY[]::text[]) AS activity_dates,
        COALESCE(cardinality(a.days), 0) AS active_days,
        COALESCE(a.previous_week, false) AS previous_week,
        COALESCE(r.pending, 0)::bigint AS pending_reminders,
        COALESCE(r.read, 0)::bigint AS reminders_read,
        COALESCE(rt.total, 0)::bigint AS reminders_total,
        COALESCE(b.upcoming, 0)::bigint AS upcoming_appointments,
        COALESCE(b.confirmed, 0)::bigint AS appointments_confirmed,
        COALESCE(b.previous_confirmed, 0)::bigint AS previous_confirmed
    FROM members e
    LEFT JOIN activity a USING (patient_user_id)
    LEFT JOIN reminders r USING (patient_user_id)
    LEFT JOIN reminders_total rt USING (patient_user_id)
    LEFT JOIN bookings b USING (patient_user_id)
)
