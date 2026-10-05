"""Organização de exames pessoais e consentimento específico para armazenamento."""
import os
import re
import uuid
from datetime import datetime, date, timezone
from typing import Optional, List, Literal
from fastapi import Depends, HTTPException, Request, UploadFile, File, Form, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api_identity import firebase_uid, require_owner
from security_defense import get_client_ip, sanitize_zero_trust_text
from config import STORAGE_EXAMS_DIR, PUBLIC_API_BASE_URL


# =============================================================
# MODELOS PYDANTIC (CONTRACT-FIRST)
# =============================================================

ExamStatus = Literal["uploaded", "analyzed", "confirmed", "failed"]
EXAM_TERMS_VERSION = "nuvem_exames_v2_20261001"


def require_exam_consent(cloud_consent, terms_version, valid_exam, purpose):
    if not cloud_consent or terms_version != EXAM_TERMS_VERSION or not valid_exam or not purpose:
        raise HTTPException(422, "Confirme o termo da Nuvem de Exames e a validade do exame antes de enviar.")


async def record_exam_consent(conn, uid, exam_id, client_ip, now):
    await conn.execute(
        """INSERT INTO exam_cloud_consents
           (user_id, exam_id, terms_version, accepted_cloud_storage,
            accepted_third_party_attack_waiver, valid_exam_attested,
            non_diagnostic_purpose_accepted, ip_address, accepted_at)
           VALUES ($1, $2, $3, TRUE, FALSE, TRUE, TRUE, $4, $5)""",
        uid, exam_id, EXAM_TERMS_VERSION, client_ip, now,
    )


class ExamItemInput(BaseModel):
    item_key: str = Field(..., max_length=100)
    item_name: str = Field(..., max_length=255)
    value_numeric: float
    value_text: str = Field(..., max_length=100)
    unit: str = Field(..., max_length=50)
    reference_text: Optional[str] = Field(None, max_length=255)
    # O mesmo contrato fechado atende rotas modernas e compatibilidade Android.
    status: ExamStatus = Field("confirmed")


class ManualExamCreateRequest(BaseModel):
    exam_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=255)
    category: str = Field("Laboratorial", max_length=100)
    exam_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: List[ExamItemInput] = Field(default_factory=list)
    replace_items: bool = False
    cloud_consent: bool = False
    terms_version: str = ""
    valid_exam_attested: bool = False
    non_diagnostic_purpose_accepted: bool = False


class LegacyExamSyncRequest(BaseModel):
    id: Optional[str] = None
    userId: str
    title: str
    category: Optional[str] = "geral"
    examDate: str
    resultSummary: Optional[str] = None
    fileUrl: Optional[str] = None
    status: ExamStatus = "uploaded"
    aiExtractedData: Optional[str] = None
    validatedBy: Optional[str] = None
    validationNotes: Optional[str] = None


class LegacyExamItemSyncRequest(BaseModel):
    id: Optional[str] = None
    examId: str
    userId: str
    itemKey: str
    itemName: str
    valueNumeric: Optional[float] = None
    valueText: Optional[str] = None
    unit: Optional[str] = None
    referenceText: Optional[str] = None
    status: ExamStatus = "uploaded"
    measuredAt: Optional[str] = None


def serialize_exam_row(row, items=None):
    d = dict(row)
    d["id"] = str(d["id"])
    if d.get("exam_date"):
        d["exam_date"] = d["exam_date"].isoformat()
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    if items is not None:
        d["items"] = items
    return d


def serialize_item_row(row):
    d = dict(row)
    d["id"] = str(d["id"])
    d["exam_id"] = str(d["exam_id"])
    if d.get("measured_at"):
        d["measured_at"] = d["measured_at"].isoformat()
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d


# =============================================================
# INSTALAÇÃO DAS ROTAS FASTAPI
# =============================================================

def install_exam_routes(app, get_pool, rate_limiter=None):
    def pool():
        p = get_pool()
        if p is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Banco de dados indisponível no momento."
            )
        return p

    @app.get("/api/exams-sync/capabilities")
    async def exam_sync_capabilities(uid: str = Depends(firebase_uid)):
        return {"snapshot_updates": True}

    # Compatibilidade Android: os endpoints /api/sync permanecem, mas usam o
    # mesmo módulo, identidade e allowlist das rotas modernas de exames.
    @app.post("/api/sync/exam")
    async def sync_exam(payload: LegacyExamSyncRequest, uid: str = Depends(firebase_uid)):
        require_owner(uid, payload.userId)
        try:
            exam_id = uuid.UUID(payload.id) if payload.id else uuid.uuid4()
        except ValueError:
            exam_id = uuid.uuid4()
        try:
            exam_date = datetime.strptime(payload.examDate, "%Y-%m-%d").date()
        except ValueError:
            exam_date = date.today()

        query = """
        INSERT INTO exams
            (id, user_id, title, category, exam_date, result_summary, file_url,
             status, ai_extracted_data, validated_by, validation_notes)
        SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
        WHERE EXISTS (SELECT 1 FROM exams WHERE id = $1 AND user_id = $2)
        ON CONFLICT (id) DO UPDATE SET
            title = EXCLUDED.title,
            category = EXCLUDED.category,
            exam_date = EXCLUDED.exam_date,
            result_summary = EXCLUDED.result_summary,
            file_url = EXCLUDED.file_url,
            status = EXCLUDED.status,
            ai_extracted_data = EXCLUDED.ai_extracted_data,
            validated_by = EXCLUDED.validated_by,
            validation_notes = EXCLUDED.validation_notes
        WHERE exams.user_id = EXCLUDED.user_id
        RETURNING id;
        """
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                query, exam_id, payload.userId, payload.title, payload.category,
                exam_date, payload.resultSummary, payload.fileUrl, payload.status,
                payload.aiExtractedData, payload.validatedBy, payload.validationNotes,
            )
        if row is None:
            raise HTTPException(status_code=403, detail="Exame pertence a outra conta.")
        return {"status": "success", "id": str(row["id"])}

    @app.post("/api/sync/exam-item")
    async def sync_exam_item(payload: LegacyExamItemSyncRequest, uid: str = Depends(firebase_uid)):
        require_owner(uid, payload.userId)
        try:
            item_id = uuid.UUID(payload.id) if payload.id else uuid.uuid4()
            exam_id = uuid.UUID(payload.examId)
        except ValueError:
            raise HTTPException(status_code=400, detail="ID de exame ou item inválido.")
        measured_at = None
        if payload.measuredAt:
            try:
                measured_at = datetime.fromisoformat(payload.measuredAt.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Data de medição inválida.")

        query = """
        INSERT INTO exam_items
            (id, exam_id, user_id, item_key, item_name, value_numeric,
             value_text, unit, reference_text, status, measured_at)
        SELECT $1, e.id, $3, $4, $5, $6, $7, $8, $9, $10, $11
          FROM exams e
         WHERE e.id = $2 AND e.user_id = $3
        ON CONFLICT (id) DO UPDATE SET
            item_key = EXCLUDED.item_key,
            item_name = EXCLUDED.item_name,
            value_numeric = EXCLUDED.value_numeric,
            value_text = EXCLUDED.value_text,
            unit = EXCLUDED.unit,
            reference_text = EXCLUDED.reference_text,
            status = EXCLUDED.status,
            measured_at = EXCLUDED.measured_at
        WHERE exam_items.user_id = EXCLUDED.user_id
        RETURNING id;
        """
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                query, item_id, exam_id, payload.userId, payload.itemKey,
                payload.itemName, payload.valueNumeric, payload.valueText,
                payload.unit, payload.referenceText, payload.status, measured_at,
            )
        if row is None:
            raise HTTPException(status_code=404, detail="Exame não encontrado para esta conta.")
        return {"status": "success", "id": str(row["id"])}

    # -------------------------------------------------------------
    # 7.1 POST /api/exams/upload — Upload de Arquivo com Consentimento LGPD
    # -------------------------------------------------------------
    @app.post("/api/exams/upload", status_code=status.HTTP_201_CREATED)
    async def upload_exam(
        request: Request,
        title: str = Form(...),
        category: str = Form("Geral"),
        exam_date: str = Form(...),
        exam_type: str = Form("LAB"),
        cloud_consent: bool = Form(False),
        terms_version: str = Form(""),
        valid_exam_attested: bool = Form(False),
        non_diagnostic_purpose_accepted: bool = Form(False),
        exam_id: Optional[str] = Form(None),
        file: UploadFile = File(...),
        uid: str = Depends(firebase_uid)
    ):
        require_exam_consent(cloud_consent, terms_version, valid_exam_attested, non_diagnostic_purpose_accepted)
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("exam_upload_ip", client_ip, limit=15, window_seconds=60, ip=client_ip, pool=pool())

        # Sanitização Zero-Trust dos campos textuais
        clean_title = sanitize_zero_trust_text(title, max_length=255, field_name="title", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        clean_category = sanitize_zero_trust_text(category, max_length=100, field_name="category", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        safe_exam_type = "IMAGE_REPORT" if exam_type.upper() in ("IMAGE_REPORT", "UNSTRUCTURED_DOCUMENT") else "LAB"

        # Validação da Data do Exame
        try:
            parsed_date = datetime.strptime(exam_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            parsed_date = date.today()

        # Resolução de UUID consistente
        try:
            parsed_exam_id = uuid.UUID(exam_id) if exam_id else uuid.uuid4()
        except ValueError:
            parsed_exam_id = uuid.uuid4()

        safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "", uid)
        now = datetime.now(timezone.utc)

        file_url = None
        storage_status = "LOCAL_ONLY"

        # Leitura e gravação do binário caso o usuário tenha consentido com nuvem
        if cloud_consent:
            # AUD-G06: antes fazia await file.read() do body INTEIRO e só depois
            # checava o tamanho — um atacante podia esgotar a RAM enviando 500MB
            # (limite só era aplicado depois do buffer). Agora rejeitamos pelo
            # Content-Length conhecido antes de materializar 1 byte.
            declared = request.headers.get("content-length")
            if declared and int(declared) > 35 * 1024 * 1024:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Arquivo excede o limite de 35MB.")

            content = await file.read()
            if not content:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="O arquivo enviado está vazio.")
            if len(content) > 35 * 1024 * 1024:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Arquivo excede o limite de 35MB.")

            original_ext = os.path.splitext(file.filename or "")[1].lower()
            signatures = {
                ".pdf": content.startswith(b"%PDF-"),
                ".jpg": content.startswith(b"\xff\xd8\xff"),
                ".jpeg": content.startswith(b"\xff\xd8\xff"),
                ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
                ".webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
            }
            if not signatures.get(original_ext, False):
                raise HTTPException(415, "Envie um exame em PDF ou uma imagem válida. A extensão deve corresponder ao arquivo.")
            user_dir = os.path.join(STORAGE_EXAMS_DIR, safe_user_id)
            os.makedirs(user_dir, exist_ok=True)

            safe_disk_filename = f"{parsed_exam_id}{original_ext}"
            file_path = os.path.join(user_dir, safe_disk_filename)

            with open(file_path, "wb") as f:
                f.write(content)

            file_url = f"{PUBLIC_API_BASE_URL}/api/files/exams/{safe_user_id}/{safe_disk_filename}"
            storage_status = "CLOUD_STORED"

        # Transação de persistência atômica no PostgreSQL
        # AUD-G11: o laudo PHI é gravado em disco ANTES da transação. Se o INSERT
        # falhar (UUID conflitado, constraint, queda do pool), o arquivo ficava
        # órfão no disco pra sempre. Agora qualquer falha na transação remove o
        # arquivo — nada de PHI sem dono no storage.
        try:
            async with pool().acquire() as conn, conn.transaction():
                await record_exam_consent(conn, uid, parsed_exam_id, client_ip, now)

                # 2. Insere ou atualiza o exame
                exam_query = """
                INSERT INTO exams
                    (id, user_id, title, category, exam_date, file_url,
                     exam_type, is_cloud_stored, cloud_consent_accepted, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'uploaded', $10)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    exam_date = EXCLUDED.exam_date,
                    file_url = COALESCE(EXCLUDED.file_url, exams.file_url),
                    exam_type = EXCLUDED.exam_type,
                    is_cloud_stored = EXCLUDED.is_cloud_stored,
                    cloud_consent_accepted = EXCLUDED.cloud_consent_accepted,
                    status = 'confirmed'
                WHERE exams.user_id = $2
                RETURNING id;
                """
                row = await conn.fetchrow(
                    exam_query,
                    parsed_exam_id, uid, clean_title, clean_category, parsed_date,
                    file_url, safe_exam_type, cloud_consent, cloud_consent, now
                )
                if row is None:
                    raise HTTPException(403, "Exame indisponível para esta conta.")
        except Exception:
            # AUD-G11: a transação falhou — não deixa laudo PHI órfão no disco.
            if cloud_consent and file_url:
                try:
                    parts = file_url.split("/api/files/exams/")[-1].split("/")
                    if len(parts) == 2:
                        u_part, f_part = parts
                        safe_u = re.sub(r"[^a-zA-Z0-9_-]", "", u_part)
                        safe_f = os.path.basename(f_part)
                        orphan = os.path.join(STORAGE_EXAMS_DIR, safe_u, safe_f)
                        if os.path.exists(orphan):
                            os.remove(orphan)
                except Exception as ex:
                    print(f"[LGPD Upload] Falha ao remover laudo órfão: {ex}")
            raise

        return {
            "success": True,
            "exam_id": str(parsed_exam_id),
            "storage_status": storage_status,
            "file_url": file_url,
            "created_at": now.isoformat()
        }

    # -------------------------------------------------------------
    # 7.2 DELETE /api/exams/{exam_id} — Exclusão Atômica Definitiva (LGPD Art. 18)
    # -------------------------------------------------------------
    @app.delete("/api/exams/{exam_id}", status_code=status.HTTP_200_OK)
    async def delete_exam(
        exam_id: str,
        request: Request,
        uid: str = Depends(firebase_uid)
    ):
        try:
            target_uuid = uuid.UUID(exam_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ID de exame inválido.")

        async with pool().acquire() as conn:
            exam = await conn.fetchrow("SELECT id, user_id, file_url FROM exams WHERE id = $1", target_uuid)
            if not exam:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exame não encontrado.")

            require_owner(uid, exam["user_id"])

            # 1. Remove arquivo físico do disco local seguro (Homelab Storage)
            file_url = exam["file_url"]
            if file_url and "/api/files/exams/" in file_url:
                try:
                    parts = file_url.split("/api/files/exams/")[-1].split("/")
                    if len(parts) == 2:
                        u_part, f_part = parts
                        safe_u = re.sub(r"[^a-zA-Z0-9_-]", "", u_part)
                        safe_f = os.path.basename(f_part)
                        disk_path = os.path.join(STORAGE_EXAMS_DIR, safe_u, safe_f)
                        if os.path.exists(disk_path):
                            os.remove(disk_path)
                except Exception as ex:
                    print(f"[LGPD Exclusão] Aviso ao deletar arquivo físico: {ex}")

            # 2. Exclusão no banco de dados (cascata remove exam_items e exam_cloud_consents)
            await conn.execute("DELETE FROM exams WHERE id = $1", target_uuid)

        return {
            "success": True,
            "exam_id": str(target_uuid),
            "message": "Exame e arquivos binários destruídos com sucesso."
        }

    # -------------------------------------------------------------
    # 7.3 POST /api/exams/manual — Cadastro de Exame Manual e Itens em Lote
    # -------------------------------------------------------------
    @app.post("/api/exams/manual", status_code=status.HTTP_201_CREATED)
    async def create_manual_exam(
        payload: ManualExamCreateRequest,
        request: Request,
        uid: str = Depends(firebase_uid)
    ):
        require_exam_consent(payload.cloud_consent, payload.terms_version,
                             payload.valid_exam_attested, payload.non_diagnostic_purpose_accepted)
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("exam_manual_ip", client_ip, limit=20, window_seconds=60, ip=client_ip, pool=pool())

        clean_title = sanitize_zero_trust_text(payload.title, max_length=255, field_name="title", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        clean_category = sanitize_zero_trust_text(payload.category, max_length=100, field_name="category", ip=client_ip, rate_limiter=rate_limiter, pool=pool())

        try:
            parsed_date = datetime.strptime(payload.exam_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            parsed_date = date.today()

        try:
            parsed_exam_id = uuid.UUID(payload.exam_id) if payload.exam_id else uuid.uuid4()
        except ValueError:
            parsed_exam_id = uuid.uuid4()

        now = datetime.now(timezone.utc)

        async with pool().acquire() as conn, conn.transaction():
            # 1. Cria o registro do exame (sem arquivo físico)
            exam_query = """
            INSERT INTO exams
                (id, user_id, title, category, exam_date, file_url,
                 exam_type, is_cloud_stored, cloud_consent_accepted, status, created_at)
            VALUES ($1, $2, $3, $4, $5, NULL, 'LAB', FALSE, TRUE, 'confirmed', $6)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                exam_date = EXCLUDED.exam_date,
                status = 'confirmed'
            WHERE exams.user_id = $2
            RETURNING id;
            """
            row = await conn.fetchrow(exam_query, parsed_exam_id, uid, clean_title, clean_category, parsed_date, now)
            if row is None:
                raise HTTPException(403, "Exame indisponível para esta conta.")
            await record_exam_consent(conn, uid, parsed_exam_id, client_ip, now)

            # Novos clientes enviam o conjunto completo. Clientes antigos mantêm o contrato anterior.
            if payload.replace_items:
                await conn.execute("DELETE FROM exam_items WHERE exam_id = $1 AND user_id = $2", parsed_exam_id, uid)

            # 2. Insere os parâmetros laboratoriais em lote
            items_count = 0
            for item in payload.items:
                item_id = uuid.uuid4()
                clean_item_key = sanitize_zero_trust_text(item.item_key, max_length=100, field_name="item_key")
                clean_item_name = sanitize_zero_trust_text(item.item_name, max_length=255, field_name="item_name")
                clean_unit = sanitize_zero_trust_text(item.unit, max_length=50, field_name="unit")
                clean_val_text = sanitize_zero_trust_text(item.value_text, max_length=100, field_name="value_text")

                item_query = """
                INSERT INTO exam_items
                    (id, exam_id, user_id, item_key, item_name,
                     value_numeric, value_text, unit, reference_text, status, measured_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'confirmed', $10, $11)
                """
                await conn.execute(
                    item_query,
                    item_id, parsed_exam_id, uid, clean_item_key, clean_item_name,
                    item.value_numeric, clean_val_text, clean_unit, item.reference_text,
                    now, now
                )
                items_count += 1

        return {
            "success": True,
            "exam_id": str(parsed_exam_id),
            "items_created_count": items_count,
            "snapshot_applied": payload.replace_items,
            "status": "confirmed"
        }

    # -------------------------------------------------------------
    # 7.4 GET /api/exams/{user_id} — Listagem Consolidada com Itens Agregados
    # -------------------------------------------------------------
    @app.get("/api/exams/{user_id}")
    async def get_user_exams(
        user_id: str,
        uid: str = Depends(firebase_uid)
    ):
        require_owner(uid, user_id)

        async with pool().acquire() as conn:
            exam_rows = await conn.fetch(
                "SELECT * FROM exams WHERE user_id = $1 ORDER BY exam_date DESC, created_at DESC",
                user_id
            )
            item_rows = await conn.fetch(
                "SELECT * FROM exam_items WHERE user_id = $1 ORDER BY measured_at DESC",
                user_id
            )

        items_by_exam = {}
        for it in item_rows:
            e_id = str(it["exam_id"])
            if e_id not in items_by_exam:
                items_by_exam[e_id] = []
            items_by_exam[e_id].append(serialize_item_row(it))

        return [serialize_exam_row(r, items_by_exam.get(str(r["id"]), [])) for r in exam_rows]

    # -------------------------------------------------------------
    # 7.5 GET /api/files/exams/{user_id}/{filename} — Servir Laudos Binários
    # -------------------------------------------------------------
    @app.get("/api/files/exams/{user_id}/{filename}")
    async def get_exam_file_content(
        user_id: str,
        filename: str,
        uid: str = Depends(firebase_uid),
    ):
        # Zero-trust: laudos sao PHI (dados de saude). Antes, quem soubesse
        # user_id + nome do arquivo baixava o exame de qualquer pessoa.
        require_owner(uid, user_id)
        safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)
        safe_filename = os.path.basename(filename)
        file_path = os.path.join(STORAGE_EXAMS_DIR, safe_user_id, safe_filename)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arquivo de laudo não encontrado.")

        ext = os.path.splitext(safe_filename)[1].lower()
        media_types = {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp"
        }
        media_type = media_types.get(ext, "application/octet-stream")

        return FileResponse(file_path, media_type=media_type, filename=safe_filename)
