SELECT jsonb_build_object(
    'periodDays', 7, 'asOf', $6::timestamptz,
    'activeUsers', count(*) FILTER (WHERE active_days > 0),
    'inactiveUsers', count(*) FILTER (WHERE active_days = 0),
    'retentionRate', round(100.0 * count(*) FILTER (WHERE previous_week AND active_days > 0)
        / NULLIF(count(*) FILTER (WHERE previous_week), 0), 1),
    'remindersRead', COALESCE(sum(reminders_read), 0),
    'dailyActiveUsers', (
        SELECT jsonb_agg(COALESCE(daily.total, 0) ORDER BY offset_day)
        FROM generate_series(-6, 0) AS offset_day
        LEFT JOIN (
            SELECT day, count(*) AS total FROM events GROUP BY day
        ) daily ON daily.day = ($6::timestamptz AT TIME ZONE 'UTC')::date + offset_day
    ),
    'appointmentsDelta', COALESCE(sum(appointments_confirmed - previous_confirmed), 0),
    'appointmentsConfirmed', COALESCE(sum(appointments_confirmed), 0)
) AS kpis FROM metrics
