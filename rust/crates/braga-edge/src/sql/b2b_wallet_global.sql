SELECT jsonb_build_object(
    'periodDays', 7, 'asOf', $6::timestamptz,
    'activeUsers', count(*) FILTER (WHERE active_days > 0),
    'inactiveUsers', count(*) FILTER (WHERE active_days = 0),
    'retentionRate', round(100.0 * count(*) FILTER (WHERE previous_week AND active_days > 0)
        / NULLIF(count(*) FILTER (WHERE previous_week), 0), 1),
    'remindersRead', COALESCE(sum(reminders_read), 0),
    'appointmentsConfirmed', COALESCE(sum(appointments_confirmed), 0)
) AS kpis FROM metrics
