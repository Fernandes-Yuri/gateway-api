"""Módulo de Gerenciamento e Persistência do Ecossistema de Exames — Gateway API.

Cumpre os contratos formais de software estipulados em:
Contexto/01_PRODUTO_E_CLINICO/08_CADERNO_DE_CONTRATOS_EXAMES_E_DOSSIE.md
- Seção 5: Termo de Consentimento LGPD com Isenção de Ataques (Art. 43, III)
- Seção 7: Rotas REST (/api/exams/upload, /api/exams/manual, /api/exams/{user_id}, /api/exams/{exam_id})
- Seção 8: Persistência no PostgreSQL RDS e Homelab Storage Local
"""
import os
import re
import uuid
from datetime import datetime, date, timezone
from typing import Optional, List
from fastapi import Depends, HTTPException, Request, UploadFile, File, Form, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api_identity import firebase_uid, require_owner
from security_defense import get_client_ip, sanitize_zero_trust_text
from config import STORAGE_EXAMS_DIR, PUBLIC_API_BASE_URL


# =============================================================
# MODELOS PYDANTIC (CONTRACT-FIRST)
# =============================================================

class ExamItemInput(BaseModel):
    item_key: str = Field(..., max_length=100)
    item_name: str = Field(..., max_length=255)
    value_numeric: float
    value_text: str = Field(..., max_length=100)
    unit: str = Field(..., max_length=50)
    reference_text: Optional[str] = Field(None, max_length=255)
    status: str = Field("confirmed", max_length=50)


class ManualExamCreateRequest(BaseModel):
    exam_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=255)
    category: str = Field("Laboratorial", max_length=100)
    exam_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: List[ExamItemInput] = Field(default_factory=list)


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
        cloud_consent: bool = Form(True),
        terms_version: str = Form("v1.0_202609"),
        exam_id: Optional[str] = Form(None),
        file: UploadFile = File(...),
        uid: str = Depends(firebase_uid)
    ):
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
            content = await file.read()
            if not content:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="O arquivo enviado está vazio.")
            if len(content) > 35 * 1024 * 1024:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Arquivo excede o limite de 35MB.")

            user_dir = os.path.join(STORAGE_EXAMS_DIR, safe_user_id)
            os.makedirs(user_dir, exist_ok=True)

            original_ext = os.path.splitext(file.filename or "")[1].lower()
            if original_ext not in [".pdf", ".jpg", ".jpeg", ".png", ".webp"]:
                original_ext = ".pdf"

            safe_disk_filename = f"{parsed_exam_id}{original_ext}"
            file_path = os.path.join(user_dir, safe_disk_filename)

            with open(file_path, "wb") as f:
                f.write(content)

            file_url = f"{PUBLIC_API_BASE_URL}/api/files/exams/{safe_user_id}/{safe_disk_filename}"
            storage_status = "CLOUD_STORED"

        # Transação de persistência atômica no PostgreSQL
        async with pool().acquire() as conn, conn.transaction():
            # 1. Registra auditoria de consentimento se nuvem aceita
            if cloud_consent:
                consent_query = """
                INSERT INTO exam_cloud_consents
                    (user_id, exam_id, terms_version, accepted_cloud_storage,
                     accepted_third_party_attack_waiver, ip_address, accepted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """
                await conn.execute(
                    consent_query,
                    uid, parsed_exam_id, terms_version, True, True, client_ip, now
                )

            # 2. Insere ou atualiza o exame
            exam_query = """
            INSERT INTO exams
                (id, user_id, title, category, exam_date, file_url,
                 exam_type, is_cloud_stored, cloud_consent_accepted, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'confirmed', $10)
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
            await conn.fetchrow(
                exam_query,
                parsed_exam_id, uid, clean_title, clean_category, parsed_date,
                file_url, safe_exam_type, cloud_consent, cloud_consent, now
            )

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
            VALUES ($1, $2, $3, $4, $5, NULL, 'LAB', FALSE, FALSE, 'confirmed', $6)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                exam_date = EXCLUDED.exam_date,
                status = 'confirmed'
            WHERE exams.user_id = $2
            RETURNING id;
            """
            await conn.fetchrow(exam_query, parsed_exam_id, uid, clean_title, clean_category, parsed_date, now)

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
