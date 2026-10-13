"""Contratos HTTP canônicos do Care OS (D62).

As correlações são associações temporais explicáveis, nunca diagnóstico ou
recomendação de tratamento. Fotos de medicamentos são apenas referência humana.
"""
import html
import json
import logging
import math
import os
import re
import unicodedata
import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from api_identity import firebase_uid
from schemas.clinical import SymptomCheckInCreate
from schemas.emergency import EmergencyTokenGrant
from schemas.medical_access import MedicalAccessGenerateRequest, MedicalAccessGrant
from config import STORAGE_DIR
from schemas.medications import (
    BarcodeMedication,
    BleTelemetryRequest,
    MedicationBatchCreate,
    MedicationBatchItemResult,
    MedicationBatchResponse,
    MedicationCreate,
    MedicationRestockRequest,
    MedicationTakeRequest,
    MedicationUpdateRequest,
    PrescriptionAnalysisResponse,
)
from services.care_os import new_public_token, require_patient_permission, row_dict, token_hash
from services.prescription_analyzer import (
    analyze_pdf_prescription,
    extract_raw_ocr_text,
    extract_structured_prescription,
    reconcile_and_verify,
)

logger = logging.getLogger(__name__)


router = APIRouter(tags=["Care OS"])
_state = SimpleNamespace(db_pool=None, public_base_url="https://api.bragasaude.online")
BRT = ZoneInfo("America/Sao_Paulo")
MEDICATION_IMAGE_DIR = os.path.join(STORAGE_DIR, "medications")
os.makedirs(MEDICATION_IMAGE_DIR, exist_ok=True)
PRESCRIPTION_STORAGE_DIR = os.path.join(STORAGE_DIR, "prescriptions")
os.makedirs(PRESCRIPTION_STORAGE_DIR, exist_ok=True)



def configure_state(*, db_pool=None, public_base_url=None):
    _state.db_pool = db_pool
    if public_base_url:
        _state.public_base_url = public_base_url.rstrip("/")


def _pool():
    if not _state.db_pool:
        raise HTTPException(503, "Banco indisponível.")
    return _state.db_pool


def _uuid_text(value):
    return str(value) if value is not None else None


def _day_bounds(day: date):
    start = datetime.combine(day, time.min, BRT)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


@router.get("/api/anvisa/medications/barcode/{ean}", response_model=BarcodeMedication)
async def medication_by_barcode(ean: str, _uid: str = Depends(firebase_uid)):
    if len(ean) != 13 or not ean.isdigit():
        raise HTTPException(422, "EAN-13 inválido.")
    async with _pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM anvisa_medication_catalog WHERE ean_barcode=$1", ean)
    if not row:
        raise HTTPException(404, "EAN não encontrado no catálogo versionado. Cadastre manualmente e confira a receita.")
    return row_dict(row)


@router.post("/api/family/patients/{patient_id}/medications", status_code=201)
async def create_medication(patient_id: str, payload: MedicationCreate, uid: str = Depends(firebase_uid)):
    pool = _pool()
    async with pool.acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medication:write")
        async with conn.transaction():
            med = await conn.fetchrow(
                """INSERT INTO medications
                   (user_id,name,dosage_mg,schedule_time,schedule_times,ean_barcode,active_principle,
                    manufacturer,pharmaceutical_form,total_units,current_units,alert_threshold_days,
                    photo_reference_url,confirmed_with_prescription,last_restock_date,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10,$11,$12,TRUE,now(),now()) RETURNING id""",
                patient_id, payload.name, payload.dosage_mg,
                payload.schedule_times[0].strftime("%H:%M"),
                ",".join(t.strftime("%H:%M") for t in payload.schedule_times),
                payload.ean_barcode, payload.active_principle, payload.manufacturer,
                payload.pharmaceutical_form, payload.total_units, payload.alert_threshold_days,
                payload.photo_reference_url,
            )
            med_id = med["id"]
            if payload.prescription:
                p = payload.prescription
                await conn.execute(
                    """INSERT INTO prescriptions
                       (medication_id,patient_id,image_url,issued_on,validity_days,prescriber_name,prescriber_crm,confirmed_by)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    med_id, patient_id, p.image_url, p.issued_on, p.validity_days,
                    p.prescriber_name, p.prescriber_crm, uid,
                )
            actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
            audit_details = {
                "medication_id": str(med_id),
                "name": payload.name,
                "ean_barcode": payload.ean_barcode,
                "anvisa_registration_number": payload.anvisa_registration_number,
                "frequency_interval_hours": payload.frequency_interval_hours,
                "treatment_duration_days": payload.treatment_duration_days,
            }
            await conn.execute(
                """INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
                   VALUES ($1,$2,$3,'MEDICATION_CREATED',$4::jsonb)""",
                patient_id, uid, actor_name, json.dumps(audit_details),
            )
    return {"id": str(med_id), "patient_id": patient_id, "confirmed_with_prescription": True}


@router.post("/api/family/patients/{patient_id}/medications/batch", response_model=MedicationBatchResponse, status_code=201)
async def create_medications_batch(
    patient_id: str,
    payload: MedicationBatchCreate,
    uid: str = Depends(firebase_uid),
):
    pool = _pool()
    async with pool.acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medication:write")
        results = []
        async with conn.transaction():
            actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
            for item in payload.medications:
                med = await conn.fetchrow(
                    """INSERT INTO medications
                       (user_id,name,dosage_mg,schedule_time,schedule_times,ean_barcode,active_principle,
                        manufacturer,pharmaceutical_form,total_units,current_units,alert_threshold_days,
                        photo_reference_url,confirmed_with_prescription,last_restock_date,updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10,$11,$12,TRUE,now(),now()) RETURNING id""",
                    patient_id, item.name, item.dosage_mg,
                    item.schedule_times[0].strftime("%H:%M"),
                    ",".join(t.strftime("%H:%M") for t in item.schedule_times),
                    item.ean_barcode, item.active_principle, item.manufacturer,
                    item.pharmaceutical_form, item.total_units, item.alert_threshold_days,
                    item.photo_reference_url,
                )
                med_id = med["id"]
                if item.prescription:
                    p = item.prescription
                    await conn.execute(
                        """INSERT INTO prescriptions
                           (medication_id,patient_id,image_url,issued_on,validity_days,prescriber_name,prescriber_crm,confirmed_by)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                        med_id, patient_id, p.image_url, p.issued_on, p.validity_days,
                        p.prescriber_name, p.prescriber_crm, uid,
                    )
                results.append(MedicationBatchItemResult(id=str(med_id), name=item.name))
            await conn.execute(
                """INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
                   VALUES ($1,$2,$3,'MEDICATION_BATCH_CREATED',$4::jsonb)""",
                patient_id, uid, actor_name, json.dumps({
                    "batch_size": len(results),
                    "medications": [r.name for r in results]
                }),
            )
    return MedicationBatchResponse(
        status="success",
        patient_id=patient_id,
        created_count=len(results),
        medications=results,
    )


@router.get("/api/family/patients/{patient_id}/medications/stock")
async def medication_stock(patient_id: str, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        rows = await conn.fetch(
            """SELECT m.*, p.issued_on, p.validity_days, c.farmacia_popular_eligible,
                      c.source_name AS catalog_source_name, c.source_checked_at AS catalog_source_checked_at,
                      CASE WHEN p.issued_on IS NULL THEN NULL ELSE p.issued_on + p.validity_days END AS prescription_expires_on
               FROM medications m LEFT JOIN prescriptions p ON p.medication_id=m.id
               LEFT JOIN anvisa_medication_catalog c ON c.ean_barcode=m.ean_barcode
               WHERE m.user_id=$1 ORDER BY m.name""", patient_id,
        )
    result = []
    for row in rows:
        item = row_dict(row)
        times = [x for x in (item.get("schedule_times") or item.get("schedule_time") or "").split(",") if x]
        daily = max(1, len(times))
        item["days_remaining"] = math.floor((item.get("current_units") or 0) / daily)
        item["is_critical"] = item["days_remaining"] <= (item.get("alert_threshold_days") or 5)
        item["id"] = _uuid_text(item["id"])
        result.append(item)
    return result


def _try_remove_stored_file(file_url_or_path: Optional[str], patient_id: str, prescription_owned: bool = False) -> None:
    """Remove com segurança o arquivo físico da receita/foto do disco (LGPD Art. 18)."""
    if not file_url_or_path or not isinstance(file_url_or_path, str):
        return
    try:
        raw_name = file_url_or_path.split("?")[0].strip()
        fname = os.path.basename(raw_name)
        if not fname or fname in (".", ".."):
            return

        if prescription_owned:
            p_path = os.path.join(PRESCRIPTION_STORAGE_DIR, fname)
            if os.path.isfile(p_path):
                os.remove(p_path)
                return

        # 2. Verifica em MEDICATION_IMAGE_DIR/<safe_patient>/
        safe_patient = re.sub(r"[^a-zA-Z0-9_-]", "", patient_id)
        m_patient_path = os.path.join(MEDICATION_IMAGE_DIR, safe_patient, fname)
        if os.path.isfile(m_patient_path):
            os.remove(m_patient_path)
            return

    except Exception as exc:
        logger.warning("[_try_remove_stored_file] Falha ao remover arquivo físico %s: %s", file_url_or_path, exc)


@router.get("/api/family/patients/{patient_id}/medications", status_code=200)
async def list_patient_medications(
    patient_id: str,
    uid: str = Depends(firebase_uid),
):
    """Retorna todos os medicamentos completos do paciente para sincronização com o app."""
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        rows = await conn.fetch(
            """SELECT id, user_id, name, active_principle, manufacturer, dosage_mg,
                      pharmaceutical_form, schedule_times, schedule_time, total_units,
                      current_units, alert_threshold_days, photo_reference_url,
                      confirmed_with_prescription, ean_barcode, NULL as anvisa_registration_number,
                      meal_context, frequency_interval_hours, treatment_duration_days,
                      last_restock_date, created_at, updated_at
               FROM medications WHERE user_id = $1 ORDER BY name ASC""",
            patient_id,
        )
        return [row_dict(r) for r in rows]


@router.get("/api/family/patients/{patient_id}/medications/logs", status_code=200)
async def list_patient_medication_logs(
    patient_id: str,
    days: int = 7,
    uid: str = Depends(firebase_uid),
):
    """Retorna os logs de doses tomadas do paciente para sincronização com o banco local Room."""
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        rows = await conn.fetch(
            """SELECT id, user_id, medication_id, taken_at, scheduled_for, actor_id, units_taken, idempotency_key
               FROM medication_logs
               WHERE user_id = $1 AND taken_at >= NOW() - make_interval(days => $2)
               ORDER BY taken_at DESC""",
            patient_id, days,
        )
    result = []
    for r in rows:
        d = row_dict(r)
        d["id"] = str(d["id"])
        d["medication_id"] = str(d["medication_id"])
        result.append(d)
    return result


@router.delete("/api/family/patients/{patient_id}/medications/{medication_id}", status_code=200)
async def delete_medication(
    patient_id: str,
    medication_id: str,
    uid: str = Depends(firebase_uid),
):
    """Exclusão de medicamento com direito ao esquecimento e descarte LGPD (Art. 18)."""
    try:
        uuid.UUID(medication_id)
    except (ValueError, AttributeError):
        raise HTTPException(404, "Medicamento não encontrado.")

    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:write")
        med = await conn.fetchrow(
            "SELECT id, name, photo_reference_url FROM medications WHERE id = $1::uuid AND user_id = $2",
            medication_id, patient_id,
        )
        if not med:
            raise HTTPException(404, "Medicamento não encontrado.")

        # Remove logs em cascata e o medicamento
        await conn.execute("DELETE FROM medication_logs WHERE medication_id = $1::uuid", medication_id)
        await conn.execute("DELETE FROM medications WHERE id = $1::uuid AND user_id = $2", medication_id, patient_id)

        # Auditoria LGPD
        actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
        audit_details = {"medication_id": medication_id, "name": med["name"], "deleted_by": uid}
        await conn.execute(
            """INSERT INTO care_audit_trail(patient_id, actor_id, actor_name, action_type, details)
               VALUES ($1, $2, $3, 'MEDICATION_DELETED', $4::jsonb)""",
            patient_id, uid, actor_name, json.dumps(audit_details),
        )

        photo_url = med.get("photo_reference_url")
        owned = False
        if photo_url:
            filename = os.path.basename(photo_url.split("?")[0])
            owned = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM prescription_files WHERE filename=$1 AND patient_id=$2) "
                "AND NOT EXISTS(SELECT 1 FROM medications WHERE photo_reference_url LIKE $3)",
                filename, patient_id, "%/" + filename)
        _try_remove_stored_file(photo_url, patient_id, prescription_owned=owned is True)
        return {"status": "success", "deleted": True, "medication_id": medication_id, "patient_id": patient_id}


@router.patch("/api/family/patients/{patient_id}/medications/{medication_id}", status_code=200)
async def update_medication(
    patient_id: str,
    medication_id: str,
    payload: MedicationUpdateRequest,
    uid: str = Depends(firebase_uid),
):
    """Edição Parcial de Horários e Posologia (PATCH)."""
    try:
        uuid.UUID(medication_id)
    except (ValueError, AttributeError):
        raise HTTPException(404, "Medicamento não encontrado.")

    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:write")
        med = await conn.fetchrow(
            "SELECT id FROM medications WHERE id = $1::uuid AND user_id = $2",
            medication_id, patient_id,
        )
        if not med:
            raise HTTPException(404, "Medicamento não encontrado.")
        updates = []
        params = [medication_id, patient_id]

        def add_param(val, col):
            params.append(val)
            updates.append(f"{col} = ${len(params)}")

        dump = payload.model_dump(exclude_unset=True)
        if "name" in dump and dump["name"]:
            add_param(dump["name"], "name")
        if "dosage_mg" in dump:
            add_param(dump["dosage_mg"], "dosage_mg")
        if "total_units" in dump and dump["total_units"] is not None:
            add_param(dump["total_units"], "total_units")
        if "alert_threshold_days" in dump and dump["alert_threshold_days"] is not None:
            add_param(dump["alert_threshold_days"], "alert_threshold_days")
        if "meal_context" in dump:
            add_param(dump["meal_context"], "meal_context")
        if "frequency_interval_hours" in dump:
            add_param(dump["frequency_interval_hours"], "frequency_interval_hours")
        if "treatment_duration_days" in dump:
            add_param(dump["treatment_duration_days"], "treatment_duration_days")
        if "schedule_times" in dump and dump["schedule_times"]:
            times = dump["schedule_times"]
            str_times = [t.strftime("%H:%M") if hasattr(t, "strftime") else str(t) for t in times]
            add_param(",".join(str_times), "schedule_times")
            add_param(str_times[0], "schedule_time")
        if updates:
            updates.append("updated_at = now()")
            query = f"UPDATE medications SET {', '.join(updates)} WHERE id = $1::uuid AND user_id = $2 RETURNING *"
            await conn.execute(query, *params)
            actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
            audit_details = {"medication_id": medication_id, "updated_fields": list(dump.keys())}
            await conn.execute(
                """INSERT INTO care_audit_trail(patient_id, actor_id, actor_name, action_type, details)
                   VALUES ($1, $2, $3, 'MEDICATION_UPDATED', $4::jsonb)""",
                patient_id, uid, actor_name, json.dumps(audit_details),
            )
        return {"status": "success", "updated": True, "medication_id": medication_id, "patient_id": patient_id}


@router.post("/api/medications/{medication_id}/take")
async def take_medication(medication_id: str, payload: MedicationTakeRequest, uid: str = Depends(firebase_uid)):
    # Passo 3.1: lógica no serviço compartilhado (endpoint REST + WhatsApp),
    # sem caminho paralelo só-com-log. A transação envolve a tomada inteira.
    from services.medication_take import take_medication as _take
    async with _pool().acquire() as conn:
        async with conn.transaction():
            return await _take(
                conn, medication_id=medication_id, actor_id=uid,
                scheduled_for=payload.scheduled_for, units_taken=payload.units_taken,
                idempotency_key=payload.idempotency_key,
            )


@router.post("/api/medications/{medication_id}/restock")
async def restock_medication(
    medication_id: str,
    payload: MedicationRestockRequest,
    uid: str = Depends(firebase_uid),
):
    """Substitui atomicamente o saldo pela quantidade conferida na nova caixa."""
    async with _pool().acquire() as conn:
        async with conn.transaction():
            med = await conn.fetchrow(
                "SELECT * FROM medications WHERE id=$1::uuid FOR UPDATE", medication_id
            )
            if not med:
                raise HTTPException(404, "Medicamento não encontrado.")
            patient_id = med["user_id"]
            await require_patient_permission(conn, uid, patient_id, "medication:write")
            existing = await conn.fetchrow(
                """SELECT id,medication_id,new_total_units FROM medication_stock_adjustments
                   WHERE patient_id=$1 AND idempotency_key=$2""",
                patient_id, payload.idempotency_key,
            )
            if existing:
                if (str(existing["medication_id"]) != str(medication_id)
                        or existing["new_total_units"] != payload.new_total_units):
                    raise HTTPException(
                        409,
                        "A chave idempotente já foi usada em outra reposição.",
                    )
                current = await conn.fetchval(
                    "SELECT current_units FROM medications WHERE id=$1::uuid", medication_id
                )
                return {"status": "already_recorded", "adjustment_id": str(existing["id"]),
                        "current_units": current, "stock_updated": False}
            adjustment = await conn.fetchrow(
                """INSERT INTO medication_stock_adjustments
                   (medication_id,patient_id,actor_id,new_total_units,idempotency_key)
                   VALUES ($1::uuid,$2,$3,$4,$5) RETURNING id,adjusted_at""",
                medication_id, patient_id, uid, payload.new_total_units, payload.idempotency_key,
            )
            current = await conn.fetchval(
                """UPDATE medications SET total_units=$2,current_units=$2,
                   last_restock_date=now(),updated_at=now()
                   WHERE id=$1::uuid RETURNING current_units""",
                medication_id, payload.new_total_units,
            )
            actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
            await conn.execute(
                """INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
                   VALUES ($1,$2,$3,'MEDICATION_RESTOCKED',$4::jsonb)""",
                patient_id, uid, actor_name,
                json.dumps({"medication_id": medication_id, "medication_name": med["name"],
                            "new_total_units": payload.new_total_units}),
            )
    return {"status": "recorded", "adjustment_id": str(adjustment["id"]),
            "current_units": current, "stock_updated": True}


@router.post("/api/family/patients/{patient_id}/medications/photo", status_code=201)
async def upload_medication_photo(
    patient_id: str,
    request: Request,
    file: UploadFile = File(...),
    uid: str = Depends(firebase_uid),
):
    """Upload autenticado de foto da caixa ou receita (referência humana)."""
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medication:write")
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > 8 * 1024 * 1024:
                raise HTTPException(413, "A foto excede o limite de 8 MB.")
        except ValueError as exc:
            raise HTTPException(400, "Content-Length inválido.") from exc
    content = await file.read()
    if not content:
        raise HTTPException(400, "A foto enviada está vazia.")
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "A foto excede o limite de 8 MB.")
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    extension = allowed.get((file.content_type or "").lower())
    if extension is None:
        raise HTTPException(415, "Formato de foto não suportado.")
    safe_patient = re.sub(r"[^a-zA-Z0-9_-]", "", patient_id)
    image_id = str(uuid.uuid4())
    directory = os.path.join(MEDICATION_IMAGE_DIR, safe_patient)
    os.makedirs(directory, exist_ok=True)
    filename = image_id + extension
    with open(os.path.join(directory, filename), "wb") as target:
        target.write(content)
    url = f"{_state.public_base_url}/api/files/medications/{safe_patient}/{filename}"
    return {"file_url": url}


@router.get("/api/files/medications/{patient_id}/{filename}")
async def medication_photo(patient_id: str, filename: str, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
    safe_patient = re.sub(r"[^a-zA-Z0-9_-]", "", patient_id)
    safe_filename = os.path.basename(filename)
    path = os.path.join(MEDICATION_IMAGE_DIR, safe_patient, safe_filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Foto não encontrada.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@router.post("/api/family/patients/{patient_id}/prescriptions/analyze", response_model=PrescriptionAnalysisResponse)
async def analyze_prescription(
    patient_id: str,
    file: UploadFile = File(...),
    request: Request = None,
    uid: str = Depends(firebase_uid),
):
    """Análise Segura de Receitas Médicas com Dupla Checagem e Árbitro ANVISA."""
    pool = _pool()
    async with pool.acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medication:write")

        if request is not None:
            declared = request.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > 10 * 1024 * 1024:
                        raise HTTPException(413, "O arquivo excede o limite de 10 MB.")
                except ValueError as exc:
                    raise HTTPException(400, "Content-Length inválido.") from exc

        content = await file.read()
        if not content:
            raise HTTPException(400, "O arquivo enviado está vazio.")
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "O arquivo excede o limite de 10 MB.")

        allowed_types = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
        content_type = (file.content_type or "").lower()
        if content_type not in allowed_types:
            ext = os.path.splitext(file.filename or "")[1].lower()
            ext_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".pdf": "application/pdf",
            }
            if ext in ext_map:
                content_type = ext_map[ext]
            elif content.startswith(b"%PDF"):
                content_type = "application/pdf"
            else:
                raise HTTPException(415, "Formato de arquivo não suportado. Envie imagem (JPEG, PNG, WebP) ou PDF.")

        raw_name = file.filename or "prescription"
        base_name = os.path.basename(raw_name)
        safe_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", base_name)
        unique_filename = f"{uuid.uuid4()}_{safe_filename}"
        target_path = os.path.join(PRESCRIPTION_STORAGE_DIR, unique_filename)
        url = f"{_state.public_base_url}/api/files/prescriptions/{unique_filename}"

        try:
            with open(target_path, "wb") as target:
                target.write(content)
            async with conn.transaction():
                # Roteamento de Pipeline: PDF Digital (Fluxo A) vs Foto Física (Fluxo B)
                is_pdf = content_type == "application/pdf" or safe_filename.lower().endswith(".pdf") or content.startswith(b"%PDF")
                if is_pdf:
                    raw_ocr = await extract_raw_ocr_text(content, safe_filename)
                    reconciled = await analyze_pdf_prescription(content, conn, raw_ocr_text=raw_ocr)
                else:
                    raw_ocr = await extract_raw_ocr_text(content, safe_filename)
                    structured = await extract_structured_prescription(content, content_type)
                    reconciled = await reconcile_and_verify(structured, raw_ocr, conn)

                await conn.execute("INSERT INTO prescription_files(filename,patient_id) VALUES ($1,$2)", unique_filename, patient_id)

                # Auditoria LGPD
                actor_name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid) or "Usuário autorizado"
                audit_details = {
                    "filename": safe_filename,
                    "content_type": content_type,
                    "size_bytes": len(content),
                    "file_url": url,
                    "medications_count": len(reconciled.medications),
                    "total_pages_analyzed": reconciled.total_pages_analyzed,
                    "duplicates_merged_count": reconciled.duplicates_merged_count,
                }
                await conn.execute(
                    """INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
                       VALUES ($1,$2,$3,'PRESCRIPTION_ANALYZED',$4::jsonb)""",
                    patient_id, uid, actor_name, json.dumps(audit_details),
                )

                return PrescriptionAnalysisResponse(
                    status="success",
                    prescription_image_url=url,
                    medications=reconciled.medications,
                    raw_ocr_snippet=raw_ocr[:500] if raw_ocr else None,
                    total_pages_analyzed=reconciled.total_pages_analyzed,
                    duplicates_merged_count=reconciled.duplicates_merged_count,
                )
        except BaseException:
            if os.path.isfile(target_path):
                os.remove(target_path)
            raise


@router.get("/api/files/prescriptions/{filename}")
async def prescription_photo(filename: str, uid: str = Depends(firebase_uid)):
    safe_filename = os.path.basename(filename)
    if safe_filename != filename:
        raise HTTPException(404, "Arquivo não encontrado.")
    async with _pool().acquire() as conn:
        patient_id = await conn.fetchval("SELECT patient_id FROM prescription_files WHERE filename=$1", filename)
        if not patient_id:
            # Anexos legados sem vínculo precisam de inventário antes de liberar acesso.
            raise HTTPException(404, "Arquivo não encontrado.")
        await require_patient_permission(conn, uid, patient_id, "care:read")
    path = os.path.join(PRESCRIPTION_STORAGE_DIR, safe_filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Arquivo não encontrado.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})



@router.get("/api/family/patients/{patient_id}/activity-feed")
async def activity_feed(patient_id: str, limit: int = Query(50, ge=1, le=100), uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        rows = await conn.fetch(
            "SELECT * FROM care_audit_trail WHERE patient_id=$1 ORDER BY occurred_at DESC LIMIT $2",
            patient_id, limit,
        )
    return [{**row_dict(r, json_fields=("details",)), "id": str(r["id"])} for r in rows]


async def _build_bulletin(conn, patient_id: str, day: date):
    start, end = _day_bounds(day)
    taken = await conn.fetchval(
        "SELECT count(*) FROM medication_logs WHERE user_id=$1 AND taken_at >= $2 AND taken_at < $3",
        patient_id, start, end,
    )
    meds = await conn.fetch("SELECT schedule_times,schedule_time FROM medications WHERE user_id=$1", patient_id)
    expected = sum(max(1, len([x for x in ((m.get("schedule_times") or m.get("schedule_time") or "").split(",")) if x])) for m in meds)
    vital = await conn.fetchrow(
        """SELECT systolic_pressure,diastolic_pressure,hydration_ml FROM vital_signs
           WHERE user_id=$1 AND measured_at >= $2 AND measured_at < $3 ORDER BY measured_at DESC LIMIT 1""",
        patient_id, start, end,
    )
    hydration = await conn.fetchval(
        "SELECT COALESCE(sum(hydration_ml),0) FROM vital_signs WHERE user_id=$1 AND measured_at >= $2 AND measured_at < $3",
        patient_id, start, end,
    )
    bp = None
    if vital and vital.get("systolic_pressure") and vital.get("diastolic_pressure"):
        bp = f'{vital["systolic_pressure"]}/{vital["diastolic_pressure"]}'
    return {"patient_id": patient_id, "day": day.isoformat(), "medications_taken": taken,
            "medications_expected": expected, "latest_blood_pressure": bp,
            "hydration_ml": hydration or 0, "privacy_scope": "CONSOLIDATED_SAFETY_ONLY"}


@router.get("/api/family/patients/{patient_id}/daily-bulletin")
async def daily_bulletin(patient_id: str, day: date | None = None, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        return await _build_bulletin(conn, patient_id, day or datetime.now(BRT).date())


@router.post("/api/symptoms/check-in", status_code=201)
async def symptom_check_in(payload: SymptomCheckInCreate, uid: str = Depends(firebase_uid)):
    patient_id = payload.patient_id or uid
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medication:take")
        row = await conn.fetchrow(
            """INSERT INTO symptoms_diary
               (patient_id,reported_by,reported_at,symptoms_text,sleep_quality,disposition,input_method)
               VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
            patient_id, uid, payload.reported_at, payload.symptoms_text.strip(), payload.sleep_quality,
            payload.disposition, payload.input_method,
        )
    return {"id": str(row["id"]), "patient_id": patient_id}


@router.get("/api/patients/{patient_id}/clinical-correlations")
async def clinical_correlations(patient_id: str, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        vitals = await conn.fetch(
            """SELECT measured_at,systolic_pressure,diastolic_pressure,glucose_level FROM vital_signs
               WHERE user_id=$1 AND measured_at >= now()-interval '30 days'
                 AND (systolic_pressure >= 160 OR diastolic_pressure >= 100 OR glucose_level >= 250)
               ORDER BY measured_at DESC LIMIT 30""", patient_id,
        )
        if not vitals:
            return {
                "patient_id": patient_id,
                "correlations": [],
                "disclaimer": "Associações temporais para revisão humana; não constituem diagnóstico nem causalidade.",
            }

        start_window = min(v["measured_at"] for v in vitals) - timedelta(hours=24)
        end_window = max(v["measured_at"] for v in vitals)

        symptoms_all = await conn.fetch(
            """SELECT symptoms_text,sleep_quality,reported_at FROM symptoms_diary
               WHERE patient_id=$1 AND reported_at BETWEEN $2 AND $3
               ORDER BY reported_at DESC""",
            patient_id, start_window, end_window,
        )

        meds_and_logs = await conn.fetch(
            """SELECT m.id AS med_id, m.name, l.taken_at
               FROM medications m
               LEFT JOIN medication_logs l
                 ON l.medication_id = m.id AND l.taken_at BETWEEN $2 AND $3
               WHERE m.user_id = $1
               ORDER BY m.name""",
            patient_id, start_window, end_window,
        )

        medications_dict = {}
        for row in meds_and_logs:
            med_id = row["med_id"]
            if med_id not in medications_dict:
                medications_dict[med_id] = {"name": row["name"], "logs": []}
            if row["taken_at"] is not None:
                medications_dict[med_id]["logs"].append(row["taken_at"])

        result = []
        for vital in vitals:
            at = vital["measured_at"]
            window_start = at - timedelta(hours=24)

            vital_symptoms = [
                s for s in symptoms_all
                if window_start <= s["reported_at"] <= at
            ][:3]

            vital_missed = []
            for med in medications_dict.values():
                taken_in_window = any(window_start <= log_time <= at for log_time in med["logs"])
                if not taken_in_window:
                    vital_missed.append(med["name"])
                    if len(vital_missed) >= 5:
                        break

            evidence = [f"Sintoma relatado: {s['symptoms_text']}" for s in vital_symptoms]
            evidence += [f"Sem dose registrada nas 24h anteriores: {name}" for name in vital_missed]
            if not evidence:
                continue

            if vital.get("systolic_pressure"):
                event = f"Pressão {vital['systolic_pressure']}/{vital['diastolic_pressure']}"
                kind = "BLOOD_PRESSURE_PEAK"
            else:
                event = f"Glicemia {vital['glucose_level']} mg/dL"
                kind = "GLUCOSE_PEAK"

            result.append({
                "event_at": at.isoformat(),
                "event_type": kind,
                "observation": f"{event} coincidiu temporalmente com registros da rotina.",
                "evidence": evidence,
                "window_hours": 24,
                "interpretation": "TEMPORAL_ASSOCIATION_NOT_DIAGNOSIS",
            })

    return {
        "patient_id": patient_id,
        "correlations": result,
        "disclaimer": "Associações temporais para revisão humana; não constituem diagnóstico nem causalidade.",
    }


@router.post("/api/patients/{patient_id}/medical-access/generate", status_code=201, response_model=MedicalAccessGrant)
async def generate_medical_access(patient_id: str, payload: MedicalAccessGenerateRequest, uid: str = Depends(firebase_uid)):
    token, digest = new_public_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "medical:share")
        await conn.execute(
            "INSERT INTO medical_access_tokens(patient_id,token_hash,created_by,purpose,expires_at) VALUES ($1,$2,$3,$4,$5)",
            patient_id, digest, uid, payload.purpose, expires,
        )
    link = f"{_state.public_base_url}/api/public/medical-dashboard/{token}"
    return {"access_token": token, "magic_link": link, "qr_code_payload": link,
            "expires_at": expires.isoformat(), "expires_in_seconds": 7200}


async def _valid_medical_token(conn, token: str):
    row = await conn.fetchrow(
        """SELECT * FROM medical_access_tokens WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at>now()""",
        token_hash(token),
    )
    if not row:
        raise HTTPException(404, "Acesso inexistente, expirado ou revogado.")
    return row


@router.get("/api/public/medical-dashboard/{access_token}", response_class=HTMLResponse)
async def medical_dashboard(access_token: str, request: Request):
    async with _pool().acquire() as conn:
        grant = await _valid_medical_token(conn, access_token)
        patient = await conn.fetchrow("SELECT id,full_name,birth_date FROM profiles WHERE id=$1", grant["patient_id"])
        meds = await conn.fetch("SELECT name,dosage_mg,current_units,schedule_times,schedule_time FROM medications WHERE user_id=$1 ORDER BY name", grant["patient_id"])
        vitals = await conn.fetchrow("""SELECT round(avg(systolic_pressure)) systolic,round(avg(diastolic_pressure)) diastolic,
            round(avg(glucose_level)) glucose,count(*) measurements FROM vital_signs
            WHERE user_id=$1 AND measured_at>=now()-interval '30 days'""", grant["patient_id"])
        doses = await conn.fetchval("SELECT count(*) FROM medication_logs WHERE user_id=$1 AND taken_at>=now()-interval '30 days'", grant["patient_id"])
        expected = 30*sum(max(1,len([x for x in (m.get('schedule_times') or m.get('schedule_time') or '').split(',') if x])) for m in meds)
        adherence = round(100*(doses or 0)/expected) if expected else None
        symptoms = await conn.fetch("SELECT reported_at,symptoms_text FROM symptoms_diary WHERE patient_id=$1 AND reported_at>=now()-interval '30 days' ORDER BY reported_at DESC LIMIT 5", grant["patient_id"])
        await conn.execute(
            "INSERT INTO medical_access_audit(token_id,ip_address,user_agent) VALUES ($1,$2,$3)",
            grant["id"], request.client.host if request.client else None, request.headers.get("user-agent", "")[:500],
        )
        await conn.execute("UPDATE medical_access_tokens SET last_accessed_at=now() WHERE id=$1", grant["id"])
    med_html = "".join(f"<li>{html.escape(m['name'])} {m.get('dosage_mg') or ''}</li>" for m in meds) or "<li>Nenhum medicamento registrado</li>"
    symptom_html = "".join(f"<li>{s['reported_at'].date()}: {html.escape(s['symptoms_text'])}</li>" for s in symptoms) or "<li>Nenhum check-in registrado</li>"
    body = f"""<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
    <title>Painel médico</title><style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#16332d}}h1{{color:#087f5b}}.notice{{background:#eefaf6;padding:12px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card{{border:1px solid #cde8df;border-radius:12px;padding:16px}}.value{{font-size:1.6rem;font-weight:700}}</style></head>
    <body><h1>Resumo clínico de {html.escape(patient['full_name'])}</h1><p class='notice'>Dados autodeclarados para apoio à consulta. Não substituem avaliação médica.</p>
    <h2>Últimos 30 dias</h2><div class='cards'><div class='card'>Pressão média<div class='value'>{vitals.get('systolic') or '-'}/{vitals.get('diastolic') or '-'}</div></div>
    <div class='card'>Glicemia média<div class='value'>{vitals.get('glucose') or '-'} mg/dL</div></div><div class='card'>Adesão registrada<div class='value'>{(str(adherence)+'%') if adherence is not None else '-'}</div></div>
    <div class='card'>Medições<div class='value'>{vitals.get('measurements') or 0}</div></div></div>
    <h2>Medicamentos em uso</h2><ul>{med_html}</ul><h2>Check-ins recentes</h2><ul>{symptom_html}</ul></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})


_UNICODE_PDF_REPLACEMENTS = {
    "\u2013": "-",   # en-dash –
    "\u2014": "--",  # em-dash —
    "\u2015": "--",  # horizontal bar ―
    "\u2018": "'",   # left single quote ‘
    "\u2019": "'",   # right single quote ’
    "\u201a": "'",   # single low-9 quote ‚
    "\u201c": '"',   # left double quote “
    "\u201d": '"',   # right double quote ”
    "\u201e": '"',   # double low-9 quote „
    "\u2026": "...", # ellipsis …
    "\u2022": "*",   # bullet •
    "\u00a0": " ",   # non-breaking space
    "\u200b": "",    # zero-width space
    "\u2122": "TM",  # trademark ™
}


def _sanitize_pdf_text(text: str) -> str:
    """Normaliza tipografia Unicode para compatibilidade estrita com PDF / Latin-1.

    Preserva acentuação padrão da língua portuguesa (á, é, í, ó, ú, ç, ã, etc.),
    substitui aspas curvas, travessões e reticências por seus equivalentes ASCII,
    e previne a emissão de interrogações brutas '?' decorrentes de encode('latin-1', 'replace').
    """
    if not text:
        return ""
    for char, repl in _UNICODE_PDF_REPLACEMENTS.items():
        text = text.replace(char, repl)

    cleaned_chars = []
    for c in text:
        if ord(c) <= 255:
            cleaned_chars.append(c)
        else:
            decomposed = unicodedata.normalize("NFKD", c)
            equiv = "".join(ch for ch in decomposed if ord(ch) <= 255 and not unicodedata.combining(ch))
            cleaned_chars.append(equiv if equiv else " ")

    clean = "".join(cleaned_chars)
    return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _minimal_pdf(lines: list[str]) -> bytes:
    content = ["BT /F1 12 Tf 50 790 Td"]
    for index, line in enumerate(lines):
        escaped = _sanitize_pdf_text(line)
        content.append(("0 -20 Td " if index else "") + f"({escaped}) Tj")
    stream = ("\n".join(content) + "\nET").encode("latin-1")
    objects = [b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
               b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
               b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources<< /Font<< /F1 5 0 R >> >> /Contents 4 0 R >>endobj\n",
               f"4 0 obj<< /Length {len(stream)} >>stream\n".encode()+stream+b"\nendstream endobj\n",
               b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"]
    pdf = bytearray(b"%PDF-1.4\n"); offsets = [0]
    for obj in objects: offsets.append(len(pdf)); pdf.extend(obj)
    xref = len(pdf); pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]: pdf.extend(f"{off:010d} 00000 n \n".encode())
    pdf.extend(f"trailer<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(pdf)


@router.get("/api/family/patients/{patient_id}/doctor-report/pdf")
async def doctor_report(patient_id: str, days: int = Query(30, ge=1, le=90), uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "care:read")
        profile = await conn.fetchrow("SELECT full_name FROM profiles WHERE id=$1", patient_id)
        bp = await conn.fetchrow("""SELECT
            round(avg(systolic_pressure) FILTER (WHERE EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo') < 12)) morning_s,
            round(avg(diastolic_pressure) FILTER (WHERE EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo') < 12)) morning_d,
            round(avg(systolic_pressure) FILTER (WHERE EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo') >= 18)) evening_s,
            round(avg(diastolic_pressure) FILTER (WHERE EXTRACT(hour FROM measured_at AT TIME ZONE 'America/Sao_Paulo') >= 18)) evening_d,
            round(100.0*count(*) FILTER (WHERE systolic_pressure<130 AND diastolic_pressure<80)/
                  NULLIF(count(*) FILTER (WHERE systolic_pressure IS NOT NULL AND diastolic_pressure IS NOT NULL),0)) target_pct,
            max(systolic_pressure) peak_s, max(diastolic_pressure) peak_d,
            round(avg(glucose_level)) glucose_avg
            FROM vital_signs WHERE user_id=$1 AND measured_at>=now()-make_interval(days => $2)
              AND (systolic_pressure IS NOT NULL OR glucose_level IS NOT NULL)""", patient_id, days)
        taken = await conn.fetchval("SELECT count(*) FROM medication_logs WHERE user_id=$1 AND taken_at>=now()-make_interval(days => $2)", patient_id, days)
        medications = await conn.fetch("SELECT schedule_times,schedule_time FROM medications WHERE user_id=$1", patient_id)
        expected = days * sum(max(1, len([x for x in (m.get('schedule_times') or m.get('schedule_time') or '').split(',') if x])) for m in medications)
        hydration = await conn.fetchval("""SELECT round(avg(total_ml)) FROM (
            SELECT (measured_at AT TIME ZONE 'America/Sao_Paulo')::date, sum(hydration_ml) total_ml
            FROM vital_signs WHERE user_id=$1 AND measured_at>=now()-make_interval(days => $2) AND hydration_ml IS NOT NULL GROUP BY 1) d""", patient_id, days)
        symptoms = await conn.fetch("SELECT reported_at,symptoms_text FROM symptoms_diary WHERE patient_id=$1 AND reported_at>=now()-make_interval(days => $2) ORDER BY reported_at DESC LIMIT 5", patient_id, days)
    adherence = round(100*(taken or 0)/expected) if expected else None
    morning = f"{bp['morning_s']}/{bp['morning_d']}" if bp and bp.get('morning_s') else "sem dados"
    evening = f"{bp['evening_s']}/{bp['evening_d']}" if bp and bp.get('evening_s') else "sem dados"
    lines = ["BRAGA SAUDE - LEVA PRO DOUTOR", f"Paciente: {profile['full_name']}", f"Periodo: ultimos {days} dias",
             f"PA media manha: {morning}; noite: {evening}; no alvo <130/80: {bp.get('target_pct') if bp else '-'}%",
             f"Maximos pressoricos registrados: {bp.get('peak_s') if bp else '-'}/{bp.get('peak_d') if bp else '-'}",
             f"Adesao registrada: {(str(adherence)+'%') if adherence is not None else 'sem posologia'} ({taken or 0}/{expected} doses)",
             f"Glicemia media: {bp.get('glucose_avg') if bp else 'sem dados'} mg/dL; hidratacao media: {hydration or 0} mL/dia",
             f"Check-ins sintomaticos: {len(symptoms)}"] + [f"- {s['reported_at'].date()}: {s['symptoms_text'][:100]}" for s in symptoms] + [
             "Dados autodeclarados. Documento de apoio; nao e diagnostico."]
    return Response(_minimal_pdf(lines), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="leva-pro-doutor-{patient_id}.pdf"', "Cache-Control": "no-store"})


@router.post("/api/patients/{patient_id}/emergency-access/generate", status_code=201, response_model=EmergencyTokenGrant)
async def generate_emergency_access(patient_id: str, uid: str = Depends(firebase_uid)):
    token, digest = new_public_token(); expires = datetime.now(timezone.utc) + timedelta(hours=24)
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, patient_id, "emergency:share")
        await conn.execute("INSERT INTO emergency_access_tokens(patient_id,token_hash,created_by,expires_at) VALUES ($1,$2,$3,$4)", patient_id, digest, uid, expires)
    return {"emergency_token": token, "rescue_link": f"{_state.public_base_url}/api/emergency/rescue-card/{token}", "expires_at": expires.isoformat()}


@router.get("/api/emergency/rescue-card/{emergency_token}", response_class=HTMLResponse)
async def rescue_card(emergency_token: str, request: Request):
    async with _pool().acquire() as conn:
        grant = await conn.fetchrow("SELECT * FROM emergency_access_tokens WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at>now()", token_hash(emergency_token))
        if not grant:
            raise HTTPException(404, "Ficha inexistente, expirada ou revogada.")
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")[:500]
        await conn.execute(
            "INSERT INTO emergency_access_audit(token_id,ip_address,user_agent) VALUES ($1,$2,$3)",
            grant["id"], client_ip, user_agent,
        )
        await conn.execute("UPDATE emergency_access_tokens SET last_accessed_at=now() WHERE id=$1", grant["id"])
        p = await conn.fetchrow("SELECT full_name,blood_type,medication_allergies,implants,emergency_contact_name,emergency_contact_phone FROM profiles WHERE id=$1", grant["patient_id"])
        meds = await conn.fetch("SELECT name,dosage_mg FROM medications WHERE user_id=$1 ORDER BY name", grant["patient_id"])
        vital = await conn.fetchrow("SELECT * FROM vital_signs WHERE user_id=$1 ORDER BY measured_at DESC LIMIT 1", grant["patient_id"])
        contacts = await conn.fetch("""SELECT p.full_name,p.phone FROM family_bindings b JOIN profiles p ON p.id=b.caregiver_id
            WHERE b.patient_id=$1 AND b.status='ACTIVE' AND p.phone IS NOT NULL ORDER BY p.full_name""", grant["patient_id"])
    allergies = ", ".join(p.get("medication_allergies") or []) or "Não informadas"
    implants = ", ".join(p.get("implants") or []) or "Não informados"
    med_text = ", ".join(f"{m['name']} {m.get('dosage_mg') or ''}" for m in meds) or "Nenhum informado"
    last = "Sem medição" if not vital else f"PA {vital.get('systolic_pressure') or '-'} / {vital.get('diastolic_pressure') or '-'}; glicemia {vital.get('glucose_level') or '-'}"
    contact_text = "; ".join(f"{c['full_name']}: {c['phone']}" for c in contacts)
    if not contact_text:
        contact_text = f"{p.get('emergency_contact_name') or 'Não informado'} — {p.get('emergency_contact_phone') or 'Não informado'}"
    body = f"""<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Ficha de resgate</title>
    <style>body{{font:18px system-ui;max-width:720px;margin:auto;padding:20px}}h1{{color:#b42318}}dt{{font-weight:bold;margin-top:14px}}</style></head><body><h1>Ficha médica de resgate</h1>
    <h2>{html.escape(p['full_name'])}</h2><dl><dt>Tipo sanguíneo</dt><dd>{html.escape(p.get('blood_type') or 'Não informado')}</dd><dt>Alergias medicamentosas</dt><dd>{html.escape(allergies)}</dd>
    <dt>Marcapasso/próteses</dt><dd>{html.escape(implants)}</dd><dt>Medicamentos contínuos</dt><dd>{html.escape(med_text)}</dd><dt>Últimas medições</dt><dd>{html.escape(last)}</dd>
    <dt>Contatos familiares</dt><dd>{html.escape(contact_text)}</dd></dl></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})


@router.post("/api/telemetry/ble", status_code=201)
async def ingest_ble(payload: BleTelemetryRequest, uid: str = Depends(firebase_uid)):
    if payload.device_type == "BLOOD_PRESSURE" and (payload.systolic_pressure is None or payload.diastolic_pressure is None):
        raise HTTPException(422, "Pressão sistólica e diastólica são obrigatórias.")
    if payload.device_type == "GLUCOSE" and payload.glucose_level is None:
        raise HTTPException(422, "Glicemia é obrigatória.")
    async with _pool().acquire() as conn:
        await require_patient_permission(conn, uid, payload.patient_id, "medication:take")
        async with conn.transaction():
            receipt = await conn.fetchrow("""INSERT INTO ble_telemetry_receipts(patient_id,device_id,device_type,protocol,measured_at)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(patient_id,device_id,measured_at) DO NOTHING RETURNING id""",
                payload.patient_id,payload.device_id,payload.device_type,payload.protocol,payload.measured_at)
            if not receipt: return {"status":"already_received"}
            vital = await conn.fetchrow("""INSERT INTO vital_signs(user_id,systolic_pressure,diastolic_pressure,glucose_level,measured_at)
                VALUES($1,$2,$3,$4,$5) RETURNING id""",payload.patient_id,payload.systolic_pressure,payload.diastolic_pressure,payload.glucose_level,payload.measured_at)
    return {"status":"recorded","receipt_id":str(receipt['id']),"vital_sign_id":str(vital['id']),"protocol":"GATT"}


# Contrato de doses v1, compartilhado com adaptadores WhatsApp.
from schemas.medication_doses import (
    DoseBatchConfirm, DoseBatchPrepare, DoseBatchResponse, DoseError,
    DoseGroupsResponse, DoseScheduleResponse, DoseScheduleUpdate,
)
from services import medication_doses as dose_service

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
import asyncpg


class DoseContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def structured_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                dose_service.fail(422, 'INVALID_INPUT', 'Confira os campos e identificadores da requisição.')
            except HTTPException as exc:
                if isinstance(exc.detail, dict) and 'code' in exc.detail:
                    raise
                dose_service.fail(exc.status_code, 'AUTHENTICATION_REQUIRED' if exc.status_code == 401 else
                                  ('INFRASTRUCTURE_UNAVAILABLE' if exc.status_code == 503 else 'REQUEST_REJECTED'),
                                  str(exc.detail), retryable=exc.status_code == 503)
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
                logger.exception('Falha de infraestrutura no contrato de doses')
                dose_service.fail(503, 'INFRASTRUCTURE_UNAVAILABLE',
                                  'Não foi possível concluir. Recupere o lote antes de tentar novamente.', retryable=True)
        return structured_handler


def dose_contract_enabled():
    return os.getenv('MEDICATION_DOSE_BATCHES_ENABLED', 'true').lower() in ('true', '1', 'yes')


async def require_dose_contract():
    if not dose_contract_enabled():
        dose_service.fail(503, 'CONTRACT_DISABLED', 'Confirmação coletiva temporariamente indisponível.', retryable=True)


dose_router = APIRouter(route_class=DoseContractRoute)

DOSE_ERRORS = {code: {"model": DoseError} for code in (403, 404, 409, 422, 503)}


@dose_router.get('/api/family/patients/{patient_id}/medications/{medication_id}/dose-schedule',
                 response_model=DoseScheduleResponse, responses=DOSE_ERRORS)
async def read_dose_schedule(patient_id: str, medication_id: uuid.UUID, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            return await dose_service.read_schedule(conn, actor_id=uid, patient_id=patient_id, medication_id=medication_id)


@dose_router.put("/api/family/patients/{patient_id}/medications/{medication_id}/dose-schedule",
            response_model=DoseScheduleResponse, responses=DOSE_ERRORS, dependencies=[Depends(require_dose_contract)])
async def update_dose_schedule(patient_id: str, medication_id: uuid.UUID,
                               payload: DoseScheduleUpdate, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            return await dose_service.update_schedule(conn, actor_id=uid, patient_id=patient_id,
                                                       medication_id=medication_id, payload=payload)


@dose_router.get("/api/family/patients/{patient_id}/medication-dose-groups",
            response_model=DoseGroupsResponse, responses=DOSE_ERRORS)
async def medication_dose_groups(patient_id: str, date: Optional[date] = None,
                                 uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            return await dose_service.materialize_groups(conn, actor_id=uid, patient_id=patient_id, day=date)


@dose_router.post("/api/family/patients/{patient_id}/medication-dose-batches", status_code=201,
             response_model=DoseBatchResponse, responses=DOSE_ERRORS, dependencies=[Depends(require_dose_contract)])
async def prepare_dose_batch(patient_id: str, payload: DoseBatchPrepare,
                             response: Response, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            result, created = await dose_service.prepare_batch(conn, actor_id=uid, patient_id=patient_id, payload=payload)
    response.status_code = 201 if created else 200
    return result


@dose_router.get("/api/medication-dose-batches/{batch_id}", response_model=DoseBatchResponse, responses=DOSE_ERRORS)
async def get_dose_batch(batch_id: uuid.UUID, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            row = await dose_service.get_batch(conn, actor_id=uid, batch_id=batch_id, lock=True)
            result = dose_service.batch_response(row)
    return result


@dose_router.post("/api/medication-dose-batches/{batch_id}/confirm", response_model=DoseBatchResponse, responses=DOSE_ERRORS, dependencies=[Depends(require_dose_contract)])
async def confirm_dose_batch(batch_id: uuid.UUID, payload: DoseBatchConfirm, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            result = await dose_service.confirm_batch(conn, actor_id=uid, batch_id=batch_id, payload=payload)
    return result


@dose_router.post("/api/medication-dose-batches/{batch_id}/cancel", response_model=DoseBatchResponse, responses=DOSE_ERRORS, dependencies=[Depends(require_dose_contract)])
async def cancel_dose_batch(batch_id: uuid.UUID, uid: str = Depends(firebase_uid)):
    async with _pool().acquire() as conn:
        async with conn.transaction():
            result = await dose_service.cancel_batch(conn, actor_id=uid, batch_id=batch_id)
    return result


@router.get('/api/medication-dose-capabilities')
async def medication_dose_capabilities(uid: str = Depends(firebase_uid)):
    return {"medication_batch_contract": 1, "enabled": dose_contract_enabled(),
            "structured_quantities": True, "integer_units_min": 1, "integer_units_max": 20,
            "proposal_ttl_seconds": 300, "offline_result_recovery": True,
            "grouping": "exact_local_time", "supported_channels": ["app", "whatsapp"]}


router.include_router(dose_router)
