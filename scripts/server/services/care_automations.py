"""Workers D62; chamadas externas são injetadas para manter testes determinísticos."""
from datetime import datetime
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")


async def _claim(conn, patient_id, caregiver_id, kind, key):
    row = await conn.fetchrow(
        """INSERT INTO care_notification_deliveries(patient_id,caregiver_id,notification_type,dedupe_key)
           VALUES($1,$2,$3,$4) ON CONFLICT(dedupe_key) DO UPDATE SET claimed_at=now()
           WHERE care_notification_deliveries.delivered_at IS NULL
             AND care_notification_deliveries.claimed_at < now()-interval '15 minutes'
           RETURNING id""",
        patient_id, caregiver_id, kind, key,
    )
    return row["id"] if row else None


async def _deliver_or_release(conn, sender, claim_id, phone, message, notification_type):
    try:
        delivered = await sender(phone, message, notification_type)
    except Exception:
        delivered = False
    if delivered is False:
        await conn.execute("DELETE FROM care_notification_deliveries WHERE id=$1", claim_id)
        return False
    await conn.execute("UPDATE care_notification_deliveries SET delivered_at=now() WHERE id=$1", claim_id)
    return True


async def send_nightly_bulletins(pool, sender):
    """Executado por cron às 20:00 BRT; envia somente agregados de segurança."""
    day = datetime.now(BRT).date()
    sent = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT b.patient_id,b.caregiver_id,c.full_name caregiver_name,c.phone,
            p.full_name patient_name FROM family_bindings b JOIN profiles c ON c.id=b.caregiver_id
            JOIN profiles p ON p.id=b.patient_id WHERE b.status='ACTIVE' AND c.phone IS NOT NULL""")
        for row in rows:
            claim = await _claim(conn,row['patient_id'],row['caregiver_id'],'NIGHTLY_BULLETIN',f"night:{row['patient_id']}:{row['caregiver_id']}:{day}")
            if not claim:
                continue
            taken = await conn.fetchval("SELECT count(*) FROM medication_logs WHERE user_id=$1 AND (taken_at AT TIME ZONE 'America/Sao_Paulo')::date=$2",row['patient_id'],day)
            vital = await conn.fetchrow("SELECT systolic_pressure,diastolic_pressure FROM vital_signs WHERE user_id=$1 AND (measured_at AT TIME ZONE 'America/Sao_Paulo')::date=$2 ORDER BY measured_at DESC LIMIT 1",row['patient_id'],day)
            pressure = f" Pressão mais recente: {vital['systolic_pressure']}/{vital['diastolic_pressure']}." if vital and vital.get('systolic_pressure') else ""
            if await _deliver_or_release(conn,sender,claim,row['phone'],f"Olá, {row['caregiver_name']}! Hoje {row['patient_name']} registrou {taken} dose(s).{pressure} Este resumo contém apenas dados de rotina e segurança.",'NIGHTLY_BULLETIN'):
                sent += 1
    return sent


async def send_pattern_break_alerts(pool, sender):
    """Sinal suave: ausência >2h após o fim da janela matinal aprendida em 14 dias."""
    now = datetime.now(BRT)
    if now.hour < 8 or now.hour >= 18:
        return 0
    sent = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch("""WITH habits AS (
          SELECT user_id, percentile_cont(.9) WITHIN GROUP (ORDER BY EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo')*60 + EXTRACT(minute FROM measured_at AT TIME ZONE 'America/Sao_Paulo')) AS end_minute
          FROM vital_signs WHERE measured_at>=now()-interval '14 days' AND EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo')<12 GROUP BY user_id)
          SELECT b.patient_id,b.caregiver_id,c.phone,c.full_name caregiver_name,p.full_name patient_name,h.end_minute
          FROM habits h JOIN family_bindings b ON b.patient_id=h.user_id AND b.status='ACTIVE'
          JOIN profiles c ON c.id=b.caregiver_id JOIN profiles p ON p.id=b.patient_id WHERE c.phone IS NOT NULL""")
        minute_now = now.hour*60+now.minute
        for row in rows:
            if minute_now <= float(row['end_minute'])+120:
                continue
            interacted = await conn.fetchval("""SELECT EXISTS(
              SELECT 1 FROM vital_signs WHERE user_id=$1 AND (measured_at AT TIME ZONE 'America/Sao_Paulo')::date=$2
              UNION ALL SELECT 1 FROM medication_logs WHERE user_id=$1 AND (taken_at AT TIME ZONE 'America/Sao_Paulo')::date=$2
              UNION ALL SELECT 1 FROM symptoms_diary WHERE patient_id=$1 AND (reported_at AT TIME ZONE 'America/Sao_Paulo')::date=$2)""",row['patient_id'],now.date())
            key=f"pattern:{row['patient_id']}:{row['caregiver_id']}:{now.date()}"
            claim = None if interacted else await _claim(conn,row['patient_id'],row['caregiver_id'],'PATTERN_BREAK',key)
            if claim and await _deliver_or_release(conn,sender,claim,row['phone'],f"Olá, {row['caregiver_name']}. {row['patient_name']} ainda não registrou a rotina da manhã hoje. Vale dar uma ligadinha para ver como está.",'PATTERN_BREAK'):
                sent += 1
    return sent


async def send_stock_alerts(pool, sender):
    sent=0; day=datetime.now(BRT).date()
    async with pool.acquire() as conn:
        rows=await conn.fetch("""SELECT m.id,m.user_id,m.name,m.current_units,m.alert_threshold_days,m.schedule_times,m.schedule_time,
          b.caregiver_id,c.phone,c.full_name caregiver_name,p.full_name patient_name,
          pr.issued_on+pr.validity_days AS prescription_expires_on
          FROM medications m JOIN family_bindings b ON b.patient_id=m.user_id AND b.status='ACTIVE'
          JOIN profiles c ON c.id=b.caregiver_id JOIN profiles p ON p.id=m.user_id
          LEFT JOIN prescriptions pr ON pr.medication_id=m.id WHERE c.phone IS NOT NULL""")
        for row in rows:
            times=[x for x in (row.get('schedule_times') or row.get('schedule_time') or '').split(',') if x]
            days=int((row.get('current_units') or 0)/max(1,len(times)))
            expiry=row.get('prescription_expires_on')
            critical=days <= (row.get('alert_threshold_days') or 5)
            expiry_soon=expiry is not None and (expiry-day).days <= 15
            if not (critical or expiry_soon): continue
            key=f"stock:{row['id']}:{row['caregiver_id']}:{day}"
            claim=await _claim(conn,row['user_id'],row['caregiver_id'],'STOCK_OR_PRESCRIPTION',key)
            if claim:
                suffix=(f" A receita vence em {expiry}." if expiry_soon else "")
                if await _deliver_or_release(conn,sender,claim,row['phone'],f"Olá, {row['caregiver_name']}! {row['name']} de {row['patient_name']} tem saldo estimado para {days} dia(s).{suffix} Confira a receita e planeje a reposição.",'STOCK_OR_PRESCRIPTION'):
                    sent+=1
    return sent
