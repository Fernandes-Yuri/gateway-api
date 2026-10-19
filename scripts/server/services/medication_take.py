"""Executor de tomada de medicamento usado pelo aplicativo e pela API REST."""
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from services.care_os import require_patient_permission

async def take_medication(conn: Any, *, medication_id: str, actor_id: str,
                          scheduled_for: datetime, units_taken: int,
                          idempotency_key: str, declared_taken_at: datetime | None = None,
                          channel: str | None = None) -> dict:
    """
    Registra a tomada de uma dose.

    Retorna {"status": "recorded", ...} ou {"status": "already_recorded", ...}.
    Erros de negócio levantam HTTPException, como no endpoint original.
    """
    if not 1 <= units_taken <= 20:
        raise HTTPException(422, "Quantidade de unidades inválida.")
    if scheduled_for.tzinfo is None:
        raise HTTPException(422, "scheduled_for deve conter fuso horário.")
    if not idempotency_key or len(idempotency_key) < 8:
        raise HTTPException(422, "Chave de idempotência inválida.")

    med = await conn.fetchrow(
        "SELECT * FROM medications WHERE id=$1::uuid FOR UPDATE", medication_id)
    if not med:
        raise HTTPException(404, "Medicamento não encontrado.")
    patient_id = med["user_id"]

    # Propriedade/permissão: autocuidado ou vínculo ativo com medication:take.
    await require_patient_permission(conn, actor_id, patient_id, "medication:take")

    from services.medication_doses import (occurrence_log, patient_profile,
                                           reserve_occurrence, fail)
    _, zone, _ = await patient_profile(conn, patient_id)
    # A ocorrência é conferida antes do saldo e independe do offset do cliente.
    existing = await occurrence_log(conn, patient_id, medication_id, scheduled_for)
    if existing:
        return {"status": "already_recorded", "log_id": str(existing["id"]),
                "stock_decremented": False, "recorded_units": existing["units_taken"],
                "historical_conflict": existing["historical_conflict"]}
    keyed = await conn.fetchrow(
        "SELECT id,medication_id,scheduled_for,units_taken FROM medication_logs WHERE user_id=$1 AND idempotency_key=$2",
        patient_id, idempotency_key)
    if keyed:
        try:
            same = (str(keyed["medication_id"]) == str(medication_id)
                    and datetime.fromisoformat(keyed["scheduled_for"]) == scheduled_for
                    and keyed["units_taken"] == units_taken)
        except (TypeError, ValueError):
            same = False
        if not same:
            fail(409, 'IDEMPOTENCY_CONFLICT', 'Chave reutilizada para uma dose diferente.')
        return {"status": "already_recorded", "log_id": str(keyed["id"]),
                "stock_decremented": False, "recorded_units": keyed["units_taken"]}
    unresolved = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM medication_dose_migration_issues i
        JOIN medication_logs l ON l.id=i.log_id WHERE l.medication_id=$1::uuid AND
        (left(l.scheduled_for,16)=$2 OR (l.scheduled_for IS NULL AND
        (l.taken_at AT TIME ZONE $3)::date=$4)))""", medication_id,
        scheduled_for.astimezone(zone).strftime('%Y-%m-%dT%H:%M'), zone.key,
        scheduled_for.astimezone(zone).date())
    if unresolved:
        fail(409, 'REVIEW_REQUIRED', 'Há um registro histórico desta dose que exige revisão.')
    if (med.get("current_units") or 0) < units_taken:
        fail(409, 'INSUFFICIENT_STOCK', 'Estoque insuficiente para registrar esta dose.')
    oid = await reserve_occurrence(conn, patient_id=patient_id, medication_id=medication_id,
                                   instant=scheduled_for, zone=zone)
    log = await conn.fetchrow(
        """INSERT INTO medication_logs
               (user_id,medication_id,taken_at,scheduled_for,actor_id,units_taken,idempotency_key,
                occurrence_id,declared_taken_at,channel,canonical_guard)
           VALUES ($1,$2::uuid,now(),$3,$4,$5,$6,$7::uuid,$8,$9,true) RETURNING id,taken_at""",
        patient_id, medication_id, scheduled_for.isoformat(), actor_id,
        units_taken, idempotency_key, oid, declared_taken_at, channel,
    )
    await conn.execute('UPDATE medication_dose_occurrences SET log_id=$2 WHERE id=$1::uuid', oid, log['id'])
    remaining = await conn.fetchval(
        "UPDATE medications SET current_units=current_units-$2,updated_at=now() "
        "WHERE id=$1::uuid RETURNING current_units",
        medication_id, units_taken,
    )
    actor_name = await conn.fetchval(
        "SELECT full_name FROM profiles WHERE id=$1", actor_id) or "Usuário autorizado"
    await conn.execute(
        """INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
           VALUES ($1,$2,$3,'MEDICATION_TAKEN',$4::jsonb)""",
        patient_id, actor_id, actor_name,
        json.dumps({"medication_id": medication_id, "medication_name": med["name"],
                    "scheduled_for": scheduled_for.isoformat(), "units_taken": units_taken}),
    )
    return {"status": "recorded", "log_id": str(log["id"]),
            "current_units": remaining, "stock_decremented": True,
            "patient_id": patient_id}
