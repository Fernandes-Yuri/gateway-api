SELECT jsonb_build_object(
    'periodDays', 7, 'asOf', $6::timestamptz,
    'activeUsers', count(*) FILTER (WHERE active_days > 0),
    'inactiveUsers', count(*) FILTER (WHERE active_days = 0),
    'retentionRate', round(100.0 * count(*) FILTER (WHERE previous_week AND active_days > 0)
        / NULLIF(count(*) FILTER (WHERE previous_week), 0), 1),
    'remindersRead', COALESCE(sum(reminders_read), 0),
    -- Denominador de "% lido" (ver b2b_wallet.sql). Contadores de badge abaixo
    -- usam 0 quando nao ha base: 0 e um valor exibivel e nao ambiguo. Somente
    -- as TAXAS (retentionRate/appointmentsDelta) devolvem NULL sem base.
    'remindersTotal', COALESCE(sum(reminders_total), 0),
    -- Apenas contagens agregadas do proprio programa ($1) e dos membros
    -- ACTIVE: nunca corpo de mensagem, nunca outro tenant.
    'unreadMessagesTotal', COALESCE((
        SELECT count(*)
          FROM program_direct_messages dm
          JOIN members m ON m.patient_user_id = dm.patient_user_id
         WHERE dm.program_id = $1
           AND dm.sender_role = 'PATIENT'
           AND dm.read_at IS NULL
           AND $7::boolean
    ), 0),
    -- PENDING e RESCHEDULE_REQUESTED sao ambos "aguardando acao do parceiro".
    -- Consulta direta (sem a janela de 14 dias do CTE appointments) para nao
    -- esconder pedido antigo ainda pendente.
    'pendingAppointmentsTotal', COALESCE((
        SELECT count(*)
          FROM program_appointments pa
          JOIN members m ON m.patient_user_id = pa.patient_user_id
         WHERE pa.program_id = $1
           AND pa.status IN ('PENDING', 'RESCHEDULE_REQUESTED')
           AND $5::boolean
    ), 0),
    'dailyActiveUsers', (
        SELECT jsonb_agg(COALESCE(daily.total, 0) ORDER BY offset_day)
        FROM generate_series(-6, 0) AS offset_day
        LEFT JOIN (
            SELECT day, count(*) AS total FROM events GROUP BY day
        ) daily ON daily.day = ($6::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date + offset_day
    ),
    -- Sem NENHUM membro com atividade na semana anterior nao existe base de
    -- comparacao: devolve NULL (e nao 0) para o app distinguir "sem base" de
    -- "igual a semana anterior". `previous_week` e o mesmo criterio usado em
    -- retentionRate, restrito a America/Sao_Paulo.
    'appointmentsDelta', CASE
        WHEN count(*) FILTER (WHERE previous_week) = 0 THEN NULL::bigint
        ELSE COALESCE(sum(appointments_confirmed - previous_confirmed), 0)
    END,
    'appointmentsConfirmed', COALESCE(sum(appointments_confirmed), 0)
) AS kpis FROM metrics
