"""Consultas somente de leitura sobre registros de tomadas de medicamento."""
import re
import unicodedata
from datetime import datetime, time, timedelta
from typing import Any

from services.health_queries import format_when, patient_timezone, zone_of
from services.operations import OperationContext, OperationResult, validate_actor


def _period(text: str, now: datetime):
    folded = unicodedata.normalize("NFD", text.lower())
    folded = "".join(char for char in folded if unicodedata.category(char) != "Mn")
    norm = re.sub(r"[^a-z0-9 ]", " ", folded)
    norm = re.sub(r"\s+", " ", norm).strip()
    day = now.date()
    if re.search(r"\bontem\b", norm):
        day -= timedelta(days=1)
    elif re.search(r"\bhoje\b", norm):
        pass
    else:
        day = None

    part = None
    if re.search(r"\b(manha|matinal)\b", norm):
        part = (time(5), time(12), "pela manhã")
    elif re.search(r"\b(tarde|vespertino)\b", norm):
        part = (time(12), time(18), "à tarde")
    elif re.search(r"\b(noite|noturno)\b", norm):
        part = (time(18), time(23, 59, 59, 999999), "à noite")
    if day is None and part is None:
        return None, None, None
    if day is None:
        day = now.date()
    start_time, end_time = (part[0], part[1]) if part else (time.min, time.max)
    start = datetime.combine(day, start_time, now.tzinfo)
    end = datetime.combine(day, end_time, now.tzinfo)
    # End exclusivizes a janela horária; para a janela diária, inclui o dia todo.
    if part:
        end += timedelta(microseconds=1)
    else:
        end = datetime.combine(day + timedelta(days=1), time.min, now.tzinfo)
    day_label = "hoje" if day == now.date() else "ontem" if day == now.date() - timedelta(days=1) else day.strftime("em %d/%m")
    label = f"{day_label} {part[2]}" if part else day_label
    return start, end, label


async def medication_take_history(conn: Any, context: OperationContext,
                                  text: str) -> OperationResult:
    """Consulta registros confirmados, sem inferir que uma dose não foi tomada."""
    denied = await validate_actor(conn, context, "care:read")
    if denied:
        return denied
    tz_name = await patient_timezone(conn, context.patient_id)
    zone = zone_of(tz_name)
    now = datetime.now(zone)
    start, end, period_label = _period(text, now)

    try:
        owner = None
        if not context.is_self_care:
            owner = await conn.fetchval(
                "SELECT full_name FROM profiles WHERE id=$1", context.patient_id)
            owner = (owner or "seu familiar").split()[0]
        if start is None:
            rows = await conn.fetch(
                """SELECT m.name, l.taken_at, l.units_taken
                   FROM medication_logs l
                   JOIN medications m ON m.id=l.medication_id AND m.user_id=l.user_id
                   WHERE l.user_id=$1
                   ORDER BY l.taken_at DESC LIMIT 1""", context.patient_id)
        else:
            rows = await conn.fetch(
                """SELECT m.name, l.taken_at, l.units_taken
                   FROM medication_logs l
                   JOIN medications m ON m.id=l.medication_id AND m.user_id=l.user_id
                   WHERE l.user_id=$1 AND l.taken_at >= $2 AND l.taken_at < $3
                   ORDER BY l.taken_at DESC LIMIT 5""",
                context.patient_id, start.astimezone(zone_of("UTC")), end.astimezone(zone_of("UTC")))
    except Exception as err:
        print(f"[Medication Queries] Falha ao consultar registros: {err}")
        return OperationResult.unavailable(
            "Não consegui consultar os registros de tomadas agora. Tente novamente em instantes.")

    who = owner or "você"
    if not rows:
        period = f" {period_label}" if period_label else " no histórico"
        return OperationResult.missing_data(
            f"Não encontrei registro de tomada de medicamento para {who}{period}. "
            "A ausência de registro não permite concluir se a pessoa tomou ou não nesse período.",
            data={"kind": "medication_take", "found": False})

    def describe(row):
        moment = row["taken_at"]
        when = format_when(moment, zone)
        name = row["name"] or "medicamento"
        return f"{name}, registrado {when}"

    entries = [describe(row) for row in rows]
    if period_label:
        detail = "; ".join(entries)
        noun = "registros de tomadas de medicamentos" if len(entries) > 1 else "registro de tomada de medicamento"
        message = f"Encontrei {noun} para {who} {period_label}: {detail}."
    else:
        message = f"O registro mais recente de tomada de medicamento para {who} é {entries[0]}."
    return OperationResult.success(
        message, data={"kind": "medication_take", "found": True,
                       "records": len(rows), "taken_at": rows[0]["taken_at"].isoformat()})
