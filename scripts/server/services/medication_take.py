"""Serviço de tomada de medicação (PR3, passo 3.1).

Lógica extraída de routers/care_os.py (POST /api/medications/{id}/take) para
um único caminho, usado pelo endpoint REST e pelo WhatsApp — sem versão
paralela só-com-log. Toda a validação (propriedade, ocorrência, registro e
estoque) mora aqui.

A transação é controlada pela chamadora: o serviço executa na conexão dada,
de modo que a tomada e a chave de operação (passo 1.4) podem entrar juntas na
mesma transação.

Levanta HTTPException nos mesmos casos do endpoint original (404/403/409/422);
o adaptador do WhatsApp converte em OperationResult.
"""
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from services.care_os import require_patient_permission


async def take_medication(conn: Any, *, medication_id: str, actor_id: str,
                          scheduled_for: datetime, units_taken: int,
                          idempotency_key: str) -> dict:
    """
    Registra a tomada de uma dose.

    Retorna {"status": "recorded", ...} ou {"status": "already_recorded", ...}.
    Erros de negócio levantam HTTPException, como no endpoint original.
    """
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

    # Ocorrência (idempotência): reenvio da mesma tomada não duplica.
    existing = await conn.fetchrow(
        "SELECT id,taken_at FROM medication_logs WHERE user_id=$1 AND idempotency_key=$2",
        patient_id, idempotency_key,
    )
    if existing:
        return {"status": "already_recorded", "log_id": str(existing["id"]),
                "stock_decremented": False}

    # Estoque: dose sem saldo não pode ser registrada.
    if (med.get("current_units") or 0) < units_taken:
        raise HTTPException(409, "Estoque insuficiente para registrar esta dose.")

    # Dose do mesmo horário já confirmada (outro cuidador ou outra via).
    duplicate = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM medication_logs
           WHERE medication_id=$1::uuid AND scheduled_for=$2)""",
        medication_id, scheduled_for.isoformat(),
    )
    if duplicate:
        raise HTTPException(409, "Esta dose já foi confirmada por outro cuidador.")

    log = await conn.fetchrow(
        """INSERT INTO medication_logs
               (user_id,medication_id,taken_at,scheduled_for,actor_id,units_taken,idempotency_key)
           VALUES ($1,$2::uuid,now(),$3,$4,$5,$6) RETURNING id,taken_at""",
        patient_id, medication_id, scheduled_for.isoformat(), actor_id,
        units_taken, idempotency_key,
    )
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
