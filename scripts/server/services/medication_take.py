"""Serviço de tomada de medicação (PR3, passos 3.1, 3.3 e 3.4).

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
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from services.care_os import require_patient_permission
from services.medication_lookup import detect_hour, identify_medication
from services.health_queries import DEFAULT_ZONE, patient_timezone
from services.operations import OperationContext, OperationResult

_METRIC_TYPE = "MED_TOMADA"

_DAY_HINTS = (
    ("anteontem", -2),
    ("ontem", -1),
    ("amanha", 1),  # normalização remove acento: cobre "amanhã"
)


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


# ------------------------------------------------------------------
# Fluxo WhatsApp: horário + confirmação (passo 3.3) e registro (passo 3.4)
# ------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


def resolve_scheduled_for(text: str, schedule_time: Optional[str],
                          zone_name: Optional[str] = None) -> Optional[datetime]:
    """
    scheduled_for (quando a dose era programada) a partir da fala ou do cadastro.

    Regra do passo 3.3: scheduled_for é separado de taken_at (este último é
    NOW() no ato da confirmação). Dia relativo (ontem/amanhã) + hora; sem hora
    na fala, usa a programação do cadastro. Sem nenhum dos dois -> None
    (esclarecer antes de gravar).
    """
    try:
        tz = ZoneInfo(zone_name or DEFAULT_ZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(DEFAULT_ZONE)
    norm = _normalize_text(text)
    hoje = datetime.now(tz).date()
    dia = hoje
    achou_dia = False
    for palavra, delta in _DAY_HINTS:
        if re.search(r"\b" + palavra + r"\b", norm):
            dia = hoje + timedelta(days=delta)
            achou_dia = True
            break
    hora = detect_hour(norm)
    if hora:
        return datetime.combine(dia, datetime.strptime(hora, "%H:%M").time(), tz)
    if re.search(r"\b(?:as|das|pelas?|para as)\s+(?:\d|vinte e quatro)|\b\d{1,2}(?:h|:)\d*", norm):
        return None
    if schedule_time:
        try:
            hora_cad = datetime.strptime(schedule_time[:5], "%H:%M").time()
        except ValueError:
            return None
        return datetime.combine(dia if achou_dia else hoje, hora_cad, tz)
    return None


def med_take_message(name: str, scheduled_for: datetime, units: int = 1) -> str:
    """Texto da proposta de confirmação: medicamento + hora, sempre do banco."""
    hora = scheduled_for.strftime("%H:%M")
    dia = scheduled_for.strftime("%d/%m/%Y")
    dose = f"{units} comprimido(s)" if units != 1 else "1 comprimido"
    return (f"💊 *Confirmação de Tomada*\n\n"
            f"Medicamento: *{name}*\n"
            f"Programado para: *{hora}* de {dia}\n"
            f"Dose: {dose}\n\n"
            f"Confirma a tomada?")


async def prepare_medication_take(conn: Any, db_pool: Any, *,
                                  context: OperationContext,
                                  text: str) -> OperationResult:
    """
    Identifica o medicamento, resolve o scheduled_for e cria a pendência de
    confirmação da tomada. A tomada em si só acontece no confirm (passo 3.4).

    Retorna confirmation (pendência criada), clarification (ambiguidade de
    medicamento ou de horário) ou missing_data (sem candidato/horário).
    """
    ident = await identify_medication(conn, context, text)
    if not ident.is_success or not ident.data:
        return ident
    med = ident.data
    scheduled_for = resolve_scheduled_for(
        text, med.get("schedule_time"), await patient_timezone(conn, context.patient_id))
    if scheduled_for is None:
        return OperationResult.clarification(
            "Não consegui saber o horário dessa dose. Me diz algo como "
            "'tomei o remédio das 8' ou confirme o horário certo.",
            data=med)
    quantity = re.search(r"\b(\d+|um|uma|dois|duas|tres|quatro)\s+(?:comprimidos?|capsulas?|unidades?)\b", _normalize_text(text))
    if quantity:
        raw = quantity.group(1)
        units = int(raw) if raw.isdigit() else {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4}[raw]
        if not 1 <= units <= 20:
            return OperationResult.clarification("Confirme a quantidade de unidades tomadas (1 a 20).")
    else:
        units = 1
    value_json = {
        "medication_id": med["medication_id"],
        "name": med["name"],
        "scheduled_for": scheduled_for.isoformat(),
        "units_taken": units,
    }
    from core.confirmation import create_pending_confirmation
    confirmation_code = await create_pending_confirmation(
        user_id=context.actor_id,
        metric_type=_METRIC_TYPE,
        value_json=value_json,
        db_pool=db_pool,
    )
    if not confirmation_code:
        return OperationResult.unavailable(
            "Não consegui preparar a confirmação da tomada agora. Tente novamente.")
    return OperationResult.confirmation(
        med_take_message(med["name"], scheduled_for, units),
        pending_op_id=confirmation_code,
        data={**med, "scheduled_for": scheduled_for.isoformat(), "units_taken": units})


async def confirm_medication_take(conn: Any, *, user_id: str,
                                  value_json: dict) -> dict:
    """
    Executa a tomada na transação do chamador (passo 3.4).

    Reenvio da mesma pendência não duplica: a pendência é consumida por
    confirm_pending_confirmation (UPDATE ... WHERE status='pending') e a
    idempotency_key deriva do scheduled_for e do medicamento.
    """
    scheduled_raw = value_json.get("scheduled_for")
    scheduled_for = datetime.fromisoformat(scheduled_raw)
    return await take_medication(
        conn,
        medication_id=value_json["medication_id"],
        actor_id=user_id,
        scheduled_for=scheduled_for,
        units_taken=int(value_json.get("units_taken") or 1),
        idempotency_key=f"wa:{value_json['medication_id']}:{scheduled_raw}",
        channel="whatsapp",
    )


async def confirm_pending_medication_take(db_pool: Any, *,
                                          confirmation_code: str,
                                          actor_id: str) -> OperationResult:
    """
    Passo 3.4: consome a pendência da tomada e executa o serviço compartilhado
    na mesma transação. Confirma ao usuário só depois do commit; reenvio do
    botão ou reenvio da Meta reconhece a mesma tomada e não duplica.

    Erros de negócio do serviço (estoque, propriedade, dose inexistente)
    voltam como resultado estruturado — o router traduz em texto, sem
    inventar sucesso.
    """
    if not db_pool:
        return OperationResult.unavailable(
            "Não consegui acessar o banco agora. Tente novamente em instantes.")
    if not confirmation_code or not actor_id:
        return OperationResult.missing_data(
            "Não consegui identificar esta confirmação. Envie novamente o "
            "remédio e o horário.")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, value_json FROM pending_confirmations
               WHERE confirmation_code = $1
                 AND user_id = $2
                 AND status = 'pending'
                 AND expires_at > NOW()""",
            confirmation_code, actor_id,
        )
        if not row:
            # Pendência já consumida (clique duplicado ou reenvio da Meta) ou
            # expirada. Sem dizer que registrou: a tomada pode não existir.
            return OperationResult.missing_data(
                "Esta confirmação já foi usada ou está expirada. Se a tomada "
                "não foi registrada, envie novamente o remédio e o horário.")

        value_json = json.loads(row["value_json"])
        pending_id = row["id"]

        outcome: dict = {}
        try:
            async with conn.transaction():
                # 1. Consome a pendência de forma atômica (passo 1.4): só um
                # clique vence; o outro encontra 'confirmed' e nada faz.
                consumed = await conn.execute(
                    """UPDATE pending_confirmations
                       SET status = 'confirmed', confirmed_at = NOW()
                       WHERE id = $1 AND status = 'pending'""",
                    pending_id,
                )
                if consumed != "UPDATE 1":
                    return OperationResult.missing_data(
                        "Esta tomada já foi confirmada por outra via. "
                        "Nada foi duplicado.")

                # 2. Executa o serviço na mesma transação: ou a pendência e a
                # tomada entram juntas, ou nenhuma entra.
                outcome = await confirm_medication_take(
                    conn, user_id=actor_id, value_json=value_json)

                # 3. Auditoria do canal, como no fluxo de saúde.
                await conn.execute(
                    """INSERT INTO confirmation_logs
                           (user_id, pending_conf_id, confirmation_code,
                            action, values_submitted, values_confirmed,
                            channel, confirmed_by_user, was_edited)
                       VALUES ($1, $2, $3, $4, $5, $6, 'whatsapp', TRUE, FALSE)""",
                    actor_id, pending_id, confirmation_code, "REGISTRAR_MED_TOMADA",
                    json.dumps(value_json), json.dumps(outcome),
                )
        except HTTPException as err:
            # Rollback automático: a pendência volta a 'pending' e nada foi
            # gravado. O detalhe do serviço vira texto — nunca "tomada feita".
            detail = getattr(err, "detail", None) or str(err)
            if isinstance(detail, dict):
                detail = detail.get("message")
            if not isinstance(detail, str) or not detail.strip():
                detail = "Não foi possível registrar a tomada agora."
            if err.status_code == 403:
                return OperationResult.denied(detail)
            return OperationResult.unavailable(detail)
        except Exception as err:
            print(f"[Med Take] Falha ao confirmar tomada: {err}")
            return OperationResult.unavailable(
                "Agora não consegui registrar a tomada. Tente novamente em instantes.")

    # Depois do commit: a tomada existe no banco.
    name = value_json.get("name") or "Medicamento"
    hora = str(value_json.get("scheduled_for") or "")[11:16]
    if outcome.get("status") == "already_recorded":
        return OperationResult.success(
            f"⏰ Esta tomada de *{name}* ({hora}) já estava registrada — "
            f"nada foi duplicado.")
    remaining = outcome.get("current_units")
    stock_line = ""
    if isinstance(remaining, (int, float)):
        stock_line = f"\n📦 Estoque restante: {remaining} comprimido(s)."
    return OperationResult.success(
        f"✅ *Tomada registrada!*\n\n"
        f"💊 {name} — confirmada às {hora}{stock_line}\n\n"
        f"O registro já aparece no seu aplicativo.")
