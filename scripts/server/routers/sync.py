"""Router de sincronizacao PostgreSQL (canal de dados do app).

Extraido do monolito server.py na Fase A passo 5
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /api/sync/*,
/api/profile/*, /api/vitals/*, /api/metrics/*, /api/social/*,
/api/feedback/*, /api/medications/*, /api/agent/receipt.

Seguranca preservada: require_owner/require_family_access em tudo que
grava ou le dados de usuario (AUD-G07: userId vem do token, nao do body),
rate-limiter por IP, sanitize_zero_trust_text em campos livres, faixa
clinica valida em vitais (SBC/SBD), status de exame em lista fechada e
LGPD Art. 18 no delete de conta (ON DELETE CASCADE).
"""
import asyncio
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from api_identity import firebase_uid, require_owner, require_family_access
from config import STORAGE_DIR, PUBLIC_API_BASE_URL
from profile_contract import profile_upsert
from community_identity import normalize_nickname
from social_time import parse_social_timestamp, serialize_social_timestamp
from security_defense import get_client_ip, sanitize_zero_trust_text
from services.content_moderation import ModerationUnavailable, moderate_avatar

router = APIRouter()
logger = logging.getLogger(__name__)


# =============================================================
# MODELOS PYDANTIC (migrados do monolito)
# =============================================================
class ProfileSyncDto(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')

    @model_validator(mode='before')
    @classmethod
    def reject_server_fields(cls, value):
        reserved = {'session_version', 'sessionVersion', 'session_revoked_at', 'isAdmin',
                    'adminRole', 'moderation_status', 'moderationStatus'}
        if isinstance(value, dict) and reserved.intersection(value):
            from security_telemetry import mark_signal
            mark_signal('forbidden_fields')
            raise ValueError('O perfil contém campo reservado ao servidor.')
        return value
    fullName: str
    birthDate: Optional[str] = None
    gender: Optional[str] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    isSmoker: Optional[bool] = False
    hasDiabetes: Optional[bool] = False
    hasHypertension: Optional[bool] = False
    hasThyroidIssue: Optional[bool] = False
    hasRenalIssue: Optional[bool] = False
    hasBoneIssue: Optional[bool] = False
    hasMuscularIssue: Optional[bool] = False
    hydrationTargetMl: Optional[int] = 2000
    dailyCalorieTarget: Optional[float] = 1800
    stepGoal: Optional[int] = 8000
    weightGoal: Optional[float] = None
    sleepStartTime: Optional[str] = "22:00"
    sleepEndTime: Optional[str] = "06:00"
    emergencyContactName: Optional[str] = None
    emergencyContactRelation: Optional[str] = None
    emergencyContactPhone: Optional[str] = None
    phone: Optional[str] = None
    consentAcceptedAt: Optional[str] = None
    notificationsEnabled: Optional[bool] = True
    locationEnabled: Optional[bool] = True
    currentXp: Optional[int] = 0
    totalXp: Optional[int] = 0
    currentLevel: Optional[int] = 1
    currentStreak: Optional[int] = 0
    showInFeed: Optional[bool] = True
    userRole: Optional[str] = None
    basicProfileComplete: bool = False
    selfCareComplete: bool = False
    selfCareSetupPending: bool = False
    diabetesType: Optional[str] = None
    activityLevel: Optional[str] = None
    foodAllergies: List[str] = Field(default_factory=list)
    customFoodRestrictions: Optional[str] = None
    caregiverMode: Optional[str] = None
    avatarIdentifier: Optional[str] = None


class VitalSignSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    systolicPressure: Optional[int] = None
    diastolicPressure: Optional[int] = None
    heartRate: Optional[int] = None
    oxygenSaturation: Optional[int] = None
    glucoseLevel: Optional[int] = None
    glucoseType: Optional[str] = None
    hydrationMl: Optional[int] = None
    measuredAt: str


class BiometrySyncDto(BaseModel):
    id: uuid.UUID
    userId: str
    weight: float = Field(ge=20, le=300, allow_inf_nan=False)
    height: Optional[float] = Field(default=None, ge=0, le=250, allow_inf_nan=False)
    measuredAt: datetime

    @field_validator("measuredAt")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("A medição precisa conter fuso horário.")
        return value


class MedicationSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    name: str
    dosageMg: Optional[float] = None
    pillQuantity: Optional[int] = None
    scheduleTime: str
    scheduleTimes: Optional[str] = None


class MedicationLogSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    medicationId: str
    takenAt: str
    scheduledFor: Optional[str] = None
    unitsTaken: int = Field(default=1, ge=1, le=20)


class DailyMetricSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    date: str
    steps: int
    distanceMeters: float
    distanceGpsMeters: Optional[float] = None
    distanceStepsMeters: Optional[float] = None
    distanceFinalMeters: Optional[float] = None
    reliabilityScore: Optional[float] = None
    caloriesBurned: float
    activeMinutes: int


class SocialPostSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    postType: str
    title: str
    description: Optional[str] = None
    relatedMilestoneId: Optional[str] = None
    createdAt: str
    # Opcional por compatibilidade: clientes legados nao enviam e a coluna
    # usa default 'PUBLIC' (023_social_posts_visibility).
    visibility: Optional[Literal["PUBLIC", "FAMILY"]] = None


class PostReactionSyncDto(BaseModel):
    postId: str
    userId: str
    reactionType: Optional[str] = "apoio"
    createdAt: str


class FeedbackSyncDto(BaseModel):
    id: Optional[str] = None
    userId: Optional[str] = None
    userEmail: Optional[str] = None
    userName: Optional[str] = None
    category: str
    title: Optional[str] = None
    message: str
    inputMethod: Optional[str] = None
    appVersion: Optional[str] = None
    deviceInfo: Optional[str] = None
    screenshotBase64: Optional[str] = Field(None, max_length=1_400_000)

    @field_validator("screenshotBase64")
    @classmethod
    def validate_screenshot(cls, value):
        if not value:
            return None
        try:
            encoded = value.split(",", 1)[-1]
            content = base64.b64decode(encoded, validate=True)
            is_image = content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or (
                content.startswith(b"RIFF") and content[8:12] == b"WEBP")
            if len(content) > 1_000_000 or not is_image:
                raise ValueError("Captura inválida ou maior que 1 MB.")
        except ValueError:
            raise ValueError("A captura deve ser uma imagem PNG, JPEG ou WebP em base64 de até 1 MB.")
        media_type = "image/png" if content.startswith(b"\x89PNG") else "image/jpeg" if content.startswith(b"\xff\xd8") else "image/webp"
        return f"data:{media_type};base64,{encoded}"


class AuditLogSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    eventType: str
    action: str
    metadataJson: Optional[str] = None


class LeagueMembershipSyncDto(BaseModel):
    id: Optional[str] = None
    userId: str
    userName: Optional[str] = None
    userLevel: Optional[int] = 1
    userStreak: Optional[int] = 0
    leagueCycleId: str
    xpEarned: Optional[int] = 0


class FeedbackReplyCreate(BaseModel):
    """doc 10 §4.1: a equipe responde um feedback."""
    feedbackId: str
    userId: str
    body: str
    author: Optional[str] = None


class AgentReceiptDto(BaseModel):
    userId: str
    action: str
    params: Dict[str, Any] = Field(default_factory=dict)
    idempotencyKey: str = Field(min_length=1, max_length=64)
    status: str = "confirmed"
    result: Optional[Dict[str, Any]] = None


# =============================================================
# PERFIL
# =============================================================
@router.post("/api/sync/profile")
async def sync_profile(p: ProfileSyncDto, request: Request, uid: str = Depends(firebase_uid)):
    require_owner(uid, p.id)
    db_pool = _state.db_pool
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("sync_profile_ip", client_ip, limit=15, window_seconds=60, ip=client_ip, pool=db_pool)
    if not db_pool:
        raise HTTPException(503, "Banco indisponível")

    # Zero-Trust sanitization
    if getattr(p, "fullName", None):
        p.fullName = sanitize_zero_trust_text(p.fullName, max_length=100, field_name="fullName", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    if getattr(p, "emergencyContactName", None):
        p.emergencyContactName = sanitize_zero_trust_text(p.emergencyContactName, max_length=100, field_name="emergencyContactName", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    if getattr(p, "phone", None):
        p.phone = sanitize_zero_trust_text(p.phone, max_length=30, field_name="phone", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    if getattr(p, "emergencyContactPhone", None):
        p.emergencyContactPhone = sanitize_zero_trust_text(p.emergencyContactPhone, max_length=30, field_name="emergencyContactPhone", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)

    try:
        query, values = profile_upsert(p)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

    async with db_pool.acquire() as conn:
        await conn.execute(query, *values)
    return {"status": "success", "id": p.id}


@router.get("/api/profile/{user_id}")
async def get_profile(user_id: str, uid: str = Depends(firebase_uid)):
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco local offline")
    async with db_pool.acquire() as conn:
        await require_family_access(conn, uid, user_id)
        row = await conn.fetchrow("SELECT * FROM profiles WHERE id = $1", user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")
        d = dict(row)

        if d.get("birth_date"):
            d["birth_date"] = d["birth_date"].isoformat()
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
        return d


@router.get("/api/profile/{user_id}/avatar")
async def profile_avatar_metadata(user_id: str, uid: str = Depends(firebase_uid)):
    """Identidade visual compartilhada; mesma audiência autenticada do arquivo de avatar.

    Não usa o endpoint clínico de perfil para exibir autores e familiares.
    A projeção explícita impede exposição dos demais campos de profiles.
    """
    if not _state.db_pool:
        raise HTTPException(503, "Banco indisponível")
    async with _state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT photo_url, avatar_identifier FROM profiles WHERE id=$1", user_id)
    if row is None:
        raise HTTPException(404, "Perfil não encontrado")
    return JSONResponse({"id": user_id, "photo_url": row.get("photo_url"),
                         "avatar_identifier": row.get("avatar_identifier")},
                        headers={"Cache-Control": "private, no-store"})


@router.delete("/api/profile/{user_id}", status_code=status.HTTP_200_OK)
async def delete_profile(user_id: str, background_tasks: BackgroundTasks, uid: str = Depends(firebase_uid)):
    require_owner(uid, user_id)
    if _state.db_pool is None:
        raise HTTPException(503, "Banco indisponível")
    from services.account_deletion import delete_account
    from services.account_lifecycle import account_contact, process_account_mail
    email, name = await account_contact(user_id)
    result = await delete_account(_state.db_pool, user_id, actor="próprio titular", receipt_email=email, receipt_name=name)
    # A solicitação explícita do titular conclui também a identidade de acesso.
    # Clientes antigos podem repetir user.delete(); UserNotFound continua idempotente.
    from firebase_admin import auth as fb_auth
    try:
        await asyncio.to_thread(fb_auth.delete_user, user_id)
    except fb_auth.UserNotFoundError:
        pass
    except Exception:
        raise HTTPException(503, "Os dados foram removidos, mas a exclusão do acesso ainda está pendente. Tente novamente para concluir.")
    background_tasks.add_task(process_account_mail, _state.db_pool)
    return result


@router.post("/api/profile/{user_id}/deactivate")
async def deactivate_profile(user_id: str, background_tasks: BackgroundTasks, uid: str = Depends(firebase_uid)):
    require_owner(uid, user_id)
    if not _state.db_pool:
        raise HTTPException(503, "Banco indisponível.")
    from services.account_lifecycle import account_contact, deactivate_account, process_account_mail
    email, name = await account_contact(user_id)
    result = await deactivate_account(_state.db_pool, user_id, email, name)
    background_tasks.add_task(process_account_mail, _state.db_pool)
    return result


@router.post("/api/profile/{user_id}/reactivate")
async def reactivate_profile(user_id: str, request: Request, uid: str = Depends(firebase_uid)):
    require_owner(uid, user_id)
    if not _state.db_pool:
        raise HTTPException(503, "Banco indisponível.")
    from services.account_lifecycle import reactivate_account
    return await reactivate_account(_state.db_pool, user_id, getattr(request.state, "firebase_auth_time", None))


# =============================================================
# FOTO DE PERFIL (avatar)
# =============================================================
AVATAR_UPLOADS_DIR = os.path.join(STORAGE_DIR, "avatars")
AVATAR_ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_FILENAME_RE = re.compile(r"^[0-9a-f-]{36}\.(jpg|png|webp)$")


def _avatar_user_dir(user_id: str) -> str:
    """Diretorio de fotos de um usuario — sempre DENTRO de AVATAR_UPLOADS_DIR.

    Defesa em profundidade contra path traversal / exclusao de terceiros:
    1) user_id e reduzido a [A-Za-z0-9_-] (sem '/', '\\', '.', '..');
    2) o caminho resultante e resolvido com realpath;
    3) exige-se que ele esteja sob AVATAR_UPLOADS_DIR — qualquer fuga
       recusa a operacao com 400 antes de tocar em disco ou banco.
    Quem chega aqui ja passou por require_owner/uuid (dono apenas).
    """
    safe_user = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)
    if not safe_user:
        raise HTTPException(status_code=400, detail="Identificador de usuário inválido.")
    root = os.path.realpath(AVATAR_UPLOADS_DIR)
    directory = os.path.realpath(os.path.join(root, safe_user))
    if not directory.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Identificador de usuário inválido.")
    return directory


def _avatar_file_path(user_id: str, filename: str) -> str:
    # basename corta qualquer tentativa de subida ('../', '..\\'); o diretorio
    # ja passou pela checagem de contencao do helper.
    return os.path.join(_avatar_user_dir(user_id), os.path.basename(filename))


def _remove_avatar_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.post("/api/profile/{user_id}/avatar", status_code=status.HTTP_201_CREATED)
async def upload_profile_avatar(
    user_id: str,
    request: Request,
    file: UploadFile = File(...),
    uid: str = Depends(firebase_uid),
):
    """Sobe a foto de perfil do proprio usuario (apenas o dono).

    Padrao ja usado nas fotos de medicamentos (care_os): upload
    autenticado, limite de 5 MB, JPEG/PNG/WebP, arquivo em disco com
    nome UUID. A foto fica visivel para qualquer usuario autenticado
    via GET /api/files/avatars (o mural mostra autores nao vinculados).
    """
    require_owner(uid, user_id)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check(
        "avatar_upload_ip", client_ip, limit=10, window_seconds=60,
        ip=client_ip, pool=db_pool,
    )

    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > AVATAR_MAX_BYTES:
                raise HTTPException(status_code=413, detail="A foto excede o limite de 5 MB.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length inválido.") from exc

    content = await file.read(AVATAR_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="A foto enviada está vazia.")
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="A foto excede o limite de 5 MB.")
    extension = AVATAR_ALLOWED_TYPES.get((file.content_type or "").lower())
    if extension is None:
        raise HTTPException(status_code=415, detail="Formato de foto não suportado (use JPEG, PNG ou WebP).")

    # Gate de moderacao (ecossistema familiar): ANTES de tocar em disco ou
    # banco. Foto aprovada = segue; reprovada = 422 com o motivo; falha de
    # visao = 503 (fail-closed, nada e gravado).
    try:
        aprovada, motivo = await moderate_avatar(content, file.content_type or "image/jpeg")
    except ModerationUnavailable:
        logger.warning("[avatar] Moderacao indisponivel para user_id=%s — upload recusado", user_id)
        raise HTTPException(
            status_code=503,
            detail="A verificação de publicação está indisponível. Sua foto atual foi mantida. Tente novamente em instantes.",
            headers={"Retry-After": "60"},
        )
    if not aprovada:
        logger.info("[avatar] Foto reprovada na moderacao")
        raise HTTPException(
            status_code=422,
            detail=motivo or "Foto não aprovada. Escolha outra imagem.",
        )

    directory = _avatar_user_dir(user_id)
    safe_user = os.path.basename(directory)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT photo_url FROM profiles WHERE id = $1", user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Perfil não encontrado.")
        os.makedirs(directory, exist_ok=True)
        filename = str(uuid.uuid4()) + extension
        with open(os.path.join(directory, filename), "wb") as target:
            target.write(content)
        photo_url = f"{PUBLIC_API_BASE_URL}/api/files/avatars/{safe_user}/{filename}"
        await conn.execute(
            "UPDATE profiles SET photo_url = $2, avatar_identifier = NULL, updated_at = NOW() WHERE id = $1",
            user_id, photo_url,
        )

    previous = row.get("photo_url")
    if previous:
        previous_name = previous.rsplit("/", 1)[-1]
        if AVATAR_FILENAME_RE.fullmatch(previous_name):
            _remove_avatar_file(os.path.join(directory, previous_name))
    return {"photo_url": photo_url}


@router.delete("/api/profile/{user_id}/avatar")
async def delete_profile_avatar(user_id: str, uid: str = Depends(firebase_uid)):
    """Remove a foto de perfil — o cliente volta ao fallback de iniciais."""
    require_owner(uid, user_id)
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    directory = _avatar_user_dir(user_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT photo_url FROM profiles WHERE id = $1", user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Perfil não encontrado.")
        await conn.execute(
            "UPDATE profiles SET photo_url = NULL, avatar_identifier = NULL, updated_at = NOW() WHERE id = $1",
            user_id,
        )
    previous = row.get("photo_url")
    if previous:
        previous_name = previous.rsplit("/", 1)[-1]
        if AVATAR_FILENAME_RE.fullmatch(previous_name):
            _remove_avatar_file(os.path.join(directory, previous_name))
    return {"status": "success", "photo_url": None}


@router.get("/api/files/avatars/{user_id}/{filename}")
async def profile_avatar_file(user_id: str, filename: str, uid: str = Depends(firebase_uid)):
    """Serve a foto de perfil para qualquer usuario autenticado.

    O nome do arquivo e um UUID inguessavel (mesma postura das fotos de
    medicamentos); nao exige vinculo familiar porque o mural mostra
    autores de outros grupos.
    """
    path = _avatar_file_path(user_id, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Foto não encontrada.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


# =============================================================
# SINAIS VITAIS
# =============================================================
@router.post("/api/sync/biometry")
async def sync_biometry(value: BiometrySyncDto, uid: str = Depends(firebase_uid)):
    require_owner(uid, value.userId)
    if not _state.db_pool:
        raise HTTPException(503, "Biometria não sincronizada: banco indisponível.")
    from services.biometry import height_in_meters, compute_imc
    height = height_in_meters(value.height)
    async with _state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO biometrics(id,user_id,weight_kg,height_m,imc,measured_at,source)
               VALUES($1,$2,$3,$4,$5,$6,'app') ON CONFLICT(id) DO NOTHING RETURNING id""",
            value.id, uid, value.weight, height, compute_imc(value.weight, height), value.measuredAt)
        if not row:
            row = await conn.fetchrow("SELECT id FROM biometrics WHERE id=$1 AND user_id=$2", value.id, uid)
            if not row:
                raise HTTPException(409, "Identificador de medição já utilizado.")
    return {"status": "success", "id": str(row["id"])}


@router.get("/api/biometry/{user_id}")
async def get_biometry(user_id: str, uid: str = Depends(firebase_uid)):
    require_owner(uid, user_id)
    if not _state.db_pool:
        raise HTTPException(503, "Biometria indisponível.")
    async with _state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM biometrics WHERE user_id=$1 ORDER BY measured_at DESC LIMIT 1000", user_id)
    return [{"id": str(r["id"]), "user_id": r["user_id"], "weight": r["weight_kg"],
             "height": r["height_m"], "imc": r["imc"], "measured_at": r["measured_at"].isoformat()}
            for r in rows]


@router.post("/api/sync/vitals")
async def sync_vital_sign(
    v: VitalSignSyncDto,
    background_tasks: BackgroundTasks,
    request: Request,
    uid: str = Depends(firebase_uid),
):
    # Zero-trust: ninguem grava sinais vitais de outro usuario. Sem isso,
    # qualquer pessoa injeta leituras falsas e dispara alertas clinicos
    # reais aos cuidadores da vitima.
    require_owner(uid, v.userId)
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode", "id": v.id or "local"}

    # Validacao de faixa clinica: valores absurdos (ex.: 99999 mmHg) eram
    # aceitos e geravam alertas sem sentido.
    def _range(val, lo, hi, nome):
        if val is None:
            return
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise HTTPException(422, f"{nome} deve ser um número.")
        if not (lo <= val <= hi):
            raise HTTPException(422, f"{nome} fora da faixa aceitável ({lo}-{hi}).")

    _range(v.systolicPressure, 40, 300, "Pressão sistólica")
    _range(v.diastolicPressure, 20, 200, "Pressão diastólica")
    _range(v.heartRate, 20, 250, "Frequência cardíaca")
    _range(v.oxygenSaturation, 30, 100, "Saturação")
    _range(v.glucoseLevel, 10, 1000, "Glicemia")

    client_ip = get_client_ip(request)
    if _state.rate_limiter:
        await _state.rate_limiter.check("sync_vitals_ip", client_ip, limit=30, window_seconds=60, ip=client_ip, pool=db_pool)

    query = """
    INSERT INTO vital_signs (user_id, systolic_pressure, diastolic_pressure, heart_rate, oxygen_saturation, glucose_level, glucose_type, hydration_ml, measured_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    RETURNING id;
    """
    try:
        m_at = datetime.fromisoformat(v.measuredAt.replace("Z", "+00:00"))
    except Exception:
        m_at = datetime.utcnow()

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                query, uid, v.systolicPressure, v.diastolicPressure,
                v.heartRate, v.oxygenSaturation, v.glucoseLevel, v.glucoseType, v.hydrationMl, m_at
            )
            inserted_id = str(row["id"])

            # Gatilho clinico (doc 10 §3.2): extraido para dispatch_clinical_alert,
            # compartilhado com a confirmacao vinda do bot do WhatsApp.
            await _state.dispatch_clinical_alert(
                db_pool,
                v.userId,
                systolic=v.systolicPressure,
                diastolic=v.diastolicPressure,
                glucose=v.glucoseLevel,
                glucose_type=v.glucoseType,
                background_tasks=background_tasks,
            )

    return {"status": "success", "id": inserted_id}


@router.get("/api/vitals/{user_id}")
async def get_vitals(
    user_id: str,
    uid: str = Depends(firebase_uid),
    limit: int = Query(50, ge=1, le=200, description="Quantidade máxima de registros a retornar"),
    offset: int = Query(0, ge=0, description="Deslocamento para paginação tradicional"),
    before: Optional[str] = Query(None, description="Cursor datetime ISO para paginação baseada em cursor")
):
    db_pool = _state.db_pool
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        await require_family_access(conn, uid, user_id)
        if before:
            try:
                b_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
                query = "SELECT * FROM vital_signs WHERE user_id = $1 AND measured_at < $2 ORDER BY measured_at DESC LIMIT $3 OFFSET $4"
                rows = await conn.fetch(query, user_id, b_dt, limit, offset)
            except Exception:
                query = "SELECT * FROM vital_signs WHERE user_id = $1 ORDER BY measured_at DESC LIMIT $2 OFFSET $3"
                rows = await conn.fetch(query, user_id, limit, offset)
        else:
            query = "SELECT * FROM vital_signs WHERE user_id = $1 ORDER BY measured_at DESC LIMIT $2 OFFSET $3"
            rows = await conn.fetch(query, user_id, limit, offset)

        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["measured_at"] = d["measured_at"].isoformat()
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            result.append(d)
        return result


# =============================================================
# METRICAS DIARIAS
# =============================================================
@router.post("/api/sync/daily-metrics")
async def sync_daily_metrics(m: DailyMetricSyncDto, uid: str = Depends(firebase_uid)):
    # AUD-G07: antes aceitava qualquer userId do body — metricas falsas podiam ser
    # injetadas em nome de outro usuario. Agora exige token Firebase e so grava
    # para o dono do token.
    if uid != m.userId:
        raise HTTPException(403, "Não autorizado: métricas só podem ser gravadas para a própria conta.")
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO daily_metrics (user_id, date, steps, distance_meters, distance_gps_meters, distance_steps_meters, distance_final_meters, reliability_score, calories_burned, active_minutes)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (user_id, date) DO UPDATE SET
        steps = EXCLUDED.steps,
        distance_meters = EXCLUDED.distance_meters,
        distance_gps_meters = EXCLUDED.distance_gps_meters,
        distance_steps_meters = EXCLUDED.distance_steps_meters,
        distance_final_meters = EXCLUDED.distance_final_meters,
        reliability_score = EXCLUDED.reliability_score,
        calories_burned = EXCLUDED.calories_burned,
        active_minutes = EXCLUDED.active_minutes
    RETURNING id;
    """
    m_date = datetime.strptime(m.date, "%Y-%m-%d").date()
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                query, m.userId, m_date, m.steps, m.distanceMeters,
                m.distanceGpsMeters, m.distanceStepsMeters, m.distanceFinalMeters,
                m.reliabilityScore, m.caloriesBurned, m.activeMinutes
            )
    return {"status": "success", "id": str(row["id"])}


@router.get("/api/metrics/daily/{user_id}")
async def get_daily_metrics(
    user_id: str,
    days: int = Query(30, ge=1, le=365, description="Número de dias/registros a retornar"),
    offset: int = Query(0, ge=0, description="Deslocamento para paginação"),
    before_date: Optional[str] = Query(None, description="Cursor de data (YYYY-MM-DD) para carregar histórico anterior"),
    uid: str = Depends(firebase_uid),
):
    require_owner(uid, user_id)
    db_pool = _state.db_pool
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        if before_date:
            try:
                b_d = datetime.strptime(before_date, "%Y-%m-%d").date()
                query = "SELECT * FROM daily_metrics WHERE user_id = $1 AND date < $2 ORDER BY date DESC LIMIT $3 OFFSET $4"
                rows = await conn.fetch(query, user_id, b_d, days, offset)
            except Exception:
                query = "SELECT * FROM daily_metrics WHERE user_id = $1 ORDER BY date DESC LIMIT $2 OFFSET $3"
                rows = await conn.fetch(query, user_id, days, offset)
        else:
            query = "SELECT * FROM daily_metrics WHERE user_id = $1 ORDER BY date DESC LIMIT $2 OFFSET $3"
            rows = await conn.fetch(query, user_id, days, offset)

        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["date"] = d["date"].isoformat()
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            result.append(d)
        return result


# =============================================================
# SOCIAL / FEED
# =============================================================
@router.post("/api/sync/social-post")
async def sync_social_post(p: SocialPostSyncDto, uid: str = Depends(firebase_uid)):
    require_owner(uid, p.userId)
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO social_posts (user_id, post_type, title, description, created_at, visibility)
    VALUES ($1, $2, $3, $4, $5, $6)
    RETURNING id;
    """
    try:
        c_at = parse_social_timestamp(p.createdAt)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Data de publicação inválida") from exc
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            query, p.userId, p.postType, p.title, p.description, c_at,
            p.visibility or "PUBLIC",
        )
    return {"status": "success", "id": str(row["id"])}


class SocialPostEditDto(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o título da publicação")
        return value


async def mutate_social_post(post_id: uuid.UUID, uid: str, edit: Optional[SocialPostEditDto] = None):
    if not _state.db_pool:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    async with _state.db_pool.acquire() as conn:
        async with conn.transaction():
            post = await conn.fetchrow(
                "SELECT user_id, is_visible FROM social_posts WHERE id = $1 FOR UPDATE", post_id,
            )
            if not post:
                raise HTTPException(status_code=404, detail="Publicação não encontrada")
            require_owner(uid, post["user_id"])
            if edit is None:
                await conn.execute("UPDATE social_posts SET is_visible = FALSE WHERE id = $1", post_id)
                return {"status": "success", "id": str(post_id)}
            if not post["is_visible"]:
                raise HTTPException(status_code=404, detail="Publicação não encontrada")
            row = await conn.fetchrow(
                "UPDATE social_posts SET title = $2, description = $3, edited_at = NOW() "
                "WHERE id = $1 RETURNING edited_at", post_id, edit.title, edit.description,
            )
    return {"status": "success", "id": str(post_id),
            "edited_at": serialize_social_timestamp(row["edited_at"])}


@router.patch("/api/sync/social-post/{post_id}")
async def edit_social_post(post_id: uuid.UUID, p: SocialPostEditDto, uid: str = Depends(firebase_uid)):
    return await mutate_social_post(post_id, uid, p)


@router.delete("/api/sync/social-post/{post_id}")
async def delete_social_post(post_id: uuid.UUID, uid: str = Depends(firebase_uid)):
    return await mutate_social_post(post_id, uid)


class CommunityNicknameDto(BaseModel):
    nickname: str = Field(default="", max_length=24)


@router.get("/api/community/profile")
async def get_community_profile(uid: str = Depends(firebase_uid)):
    if not _state.db_pool:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    async with _state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT community_nickname FROM profiles WHERE id = $1", uid)
    if not row:
        raise HTTPException(status_code=404, detail="Perfil não encontrado")
    return {"nickname": row["community_nickname"] or ""}


@router.post("/api/community/profile")
async def save_community_profile(p: CommunityNicknameDto, uid: str = Depends(firebase_uid)):
    try:
        nickname = normalize_nickname(p.nickname)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not _state.db_pool:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    await _state.rate_limiter.check("community_nickname", uid, limit=10, window_seconds=60, pool=_state.db_pool)
    async with _state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE profiles SET community_nickname = $2, updated_at = NOW() WHERE id = $1 RETURNING community_nickname",
            uid, nickname or None,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Perfil não encontrado")
    return {"nickname": row["community_nickname"] or ""}


@router.get("/api/social/feed")
async def get_social_feed(
    limit: int = Query(30, ge=1, le=100, description="Máximo 100 posts por página"),
    offset: int = Query(0, ge=0, description="Deslocamento para paginação"),
    uid: str = Depends(firebase_uid),
):
    # AUD-G08: antes era anonimo (sem firebase_uid) e aceitava limit sem teto
    # (?limit=1000000) — dump de full_name de pacientes por qualquer um. Agora
    # exige conta logada e o limit e forcado a <=100 pela validacao do Query.
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(503, "Mural temporariamente indisponível. Tente novamente.")
    query = """
    SELECT sp.id, sp.user_id, sp.post_type, sp.title, sp.description, sp.created_at, sp.edited_at,
           sp.visibility,
           p.full_name as user_name, p.community_nickname as user_nickname, p.avatar_identifier, p.photo_url,
           (SELECT COUNT(*) FROM post_reactions pr WHERE pr.post_id = sp.id) as reaction_count,
           EXISTS(SELECT 1 FROM post_reactions pr
                  WHERE pr.post_id = sp.id AND pr.user_id = $3) as has_user_reacted
    FROM social_posts sp
    JOIN profiles p ON sp.user_id = p.id
    WHERE sp.is_visible = TRUE
      AND (
          sp.visibility = 'PUBLIC'
          OR sp.user_id = $3
          OR EXISTS (
              SELECT 1 FROM family_bindings b
              WHERE b.status = 'ACTIVE'
                AND (
                    (b.patient_id = sp.user_id AND b.caregiver_id = $3)
                    OR (b.caregiver_id = sp.user_id AND b.patient_id = $3)
                )
          )
      )
    ORDER BY sp.created_at DESC
    LIMIT $1 OFFSET $2;
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, limit, offset, uid)
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["created_at"] = serialize_social_timestamp(d["created_at"])
            d["edited_at"] = serialize_social_timestamp(d["edited_at"]) if d.get("edited_at") else None
            result.append(d)
        return result


@router.post("/api/sync/post-reaction")
async def sync_post_reaction(r: PostReactionSyncDto, uid: str = Depends(firebase_uid)):
    # AUD-G07: antes aceitava qualquer userId do body — permitia forjar reacoes
    # em nome de outro usuario. Agora exige token Firebase e valida dono.
    if uid != r.userId:
        raise HTTPException(403, "Não autorizado: reações só podem ser gravadas para a própria conta.")
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO post_reactions (post_id, user_id, reaction_type, created_at)
    VALUES ($1::uuid, $2, $3, $4)
    ON CONFLICT (post_id, user_id) DO NOTHING;
    """
    try:
        c_at = datetime.fromisoformat(r.createdAt.replace("Z", "+00:00"))
    except Exception:
        c_at = datetime.utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(query, r.postId, r.userId, r.reactionType or "apoio", c_at)
    return {"status": "success"}


# =============================================================
# FEEDBACK
# =============================================================
@router.post("/api/sync/feedback")
async def sync_feedback(f: FeedbackSyncDto, request: Request, uid: str = Depends(firebase_uid)):
    # AUD-G07: antes confia em f.userId do body — feedbacks podiam ser forjados
    # em nome de outro usuario (e no e-mail de contato). Agora o userId vem do
    # token; o e-mail/name do body sao sanitizados e truncados por seguranca.
    if uid != f.userId:
        raise HTTPException(403, "Não autorizado: feedbacks só podem ser enviados para a própria conta.")
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(503, "Feedback não persistido: banco indisponível.")
    try:
        feedback_id = uuid.UUID(f.id) if f.id else uuid.uuid4()
    except ValueError:
        raise HTTPException(422, "Identificador de feedback inválido.")
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("sync_feedback_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=db_pool)

    clean_title = sanitize_zero_trust_text(f.title, max_length=200, field_name="title", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    clean_message = sanitize_zero_trust_text(f.message, max_length=1000, field_name="message", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    clean_email = sanitize_zero_trust_text(f.userEmail or "", max_length=254, field_name="userEmail", ip=client_ip)
    clean_name = sanitize_zero_trust_text(f.userName or "", max_length=120, field_name="userName", ip=client_ip)

    query = """
    INSERT INTO feedbacks (id, user_id, user_email, user_name, category, title, message, input_method, app_version, device_info, screenshot_base64, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
    ON CONFLICT (id) DO NOTHING RETURNING id;
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(query, feedback_id, uid, clean_email, clean_name, f.category, clean_title, clean_message, f.inputMethod, f.appVersion, f.deviceInfo, f.screenshotBase64)
        if row is None:
            row = await conn.fetchrow("SELECT id FROM feedbacks WHERE id=$1 AND user_id=$2", feedback_id, uid)
            if row is None:
                raise HTTPException(409, "Identificador de feedback já utilizado.")
    return {"status": "success", "persisted": True, "id": str(row["id"])}


@router.post("/api/feedback/reply")
async def create_feedback_reply(req: FeedbackReplyCreate, request: Request, uid: str = Depends(firebase_uid)):
    # A rota legada aceitava qualquer usuário Firebase como equipe de suporte.
    # O portal usa exclusivamente a rota administrativa autenticada.
    raise HTTPException(410, "Respostas da equipe exigem o canal administrativo do suporte.")


@router.get("/api/feedback/replies")
async def list_feedback_replies(uid: str = Depends(firebase_uid)):
    """Pull incremental das respostas ainda nao entregues (doc 10 §4.1).

    Marca como entregue no ato da leitura — a proxima chamada nao repete.
    """
    db_pool = _state.db_pool
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, feedback_id, author, body, created_at FROM feedback_replies "
            "WHERE user_id=$1 AND delivered_at IS NULL ORDER BY created_at ASC", uid)
        if rows:
            await conn.execute(
                "UPDATE feedback_replies SET delivered_at=NOW() WHERE user_id=$1 AND delivered_at IS NULL", uid)
    return [{
        "id": str(r["id"]),
        "feedbackId": str(r["feedback_id"]),
        "author": r["author"],
        "body": r["body"],
        "createdAt": r["created_at"].isoformat(),
    } for r in rows]



@router.post("/api/sync/medication")
async def sync_medication(m: MedicationSyncDto, uid: str = Depends(firebase_uid)):
    require_owner(uid, m.userId)
    if m.scheduleTimes is not None:
        times = sorted(set(part.strip() for part in m.scheduleTimes.split(",")))
        if not times or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
            raise HTTPException(422, "Horários inválidos.")
        m.scheduleTimes = ",".join(times)
        m.scheduleTime = times[0]
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO medications (id, user_id, name, dosage_mg, pill_quantity, schedule_time, schedule_times, updated_at)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, NOW())
    ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name,
        dosage_mg = EXCLUDED.dosage_mg,
        pill_quantity = EXCLUDED.pill_quantity,
        schedule_time = CASE WHEN EXCLUDED.schedule_times IS NULL AND medications.schedule_times IS NOT NULL THEN medications.schedule_time ELSE EXCLUDED.schedule_time END,
        schedule_times = COALESCE(EXCLUDED.schedule_times, medications.schedule_times),
        updated_at = NOW()
    WHERE medications.user_id = EXCLUDED.user_id
    RETURNING id;
    """
    med_id = m.id or str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            query, med_id, m.userId, m.name, m.dosageMg, m.pillQuantity, m.scheduleTime, m.scheduleTimes
        )
    if row is None:
        raise HTTPException(403, "Medicamento indisponível.")
    return {"status": "success", "id": str(row["id"]), "medicationContract": 2}


@router.post("/api/sync/medication-log")
async def sync_medication_log(l: MedicationLogSyncDto, uid: str = Depends(firebase_uid)):
    require_owner(uid, l.userId)
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO medication_logs (id, user_id, medication_id, taken_at, scheduled_for)
    VALUES ($1::uuid, $2, $3::uuid, $4, $5)
    ON CONFLICT (id) DO NOTHING
    RETURNING id;
    """
    log_id = l.id or str(uuid.uuid4())
    try:
        t_at = datetime.fromisoformat(l.takenAt.replace("Z", "+00:00"))
    except Exception:
        t_at = datetime.utcnow()
    async with db_pool.acquire() as conn:
        owned = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM medications WHERE id=$1::uuid AND user_id=$2)", l.medicationId, uid)
        if not owned:
            raise HTTPException(403, "Medicamento indisponível.")
        if l.scheduledFor is not None:
            from services.medication_doses import patient_profile, instant_for
            from services.medication_take import take_medication
            try:
                scheduled = datetime.fromisoformat(l.scheduledFor.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(422, "Identificador de dose inválido.")
            if scheduled.tzinfo is None:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', l.scheduledFor):
                    raise HTTPException(422, "Identificador de dose inválido.")
                _, zone, _ = await patient_profile(conn, uid)
                scheduled = instant_for(scheduled.date(), scheduled.strftime('%H:%M'), zone)
            async with conn.transaction():
                result = await take_medication(conn, medication_id=l.medicationId, actor_id=uid,
                    scheduled_for=scheduled, units_taken=l.unitsTaken,
                    idempotency_key=f'sync:{log_id}', channel='app')
            return {"status": "success", "id": result['log_id'], "medicationContract": 2,
                    "medication_batch_contract": 1, "dose_status": result['status']}
        row = await conn.fetchrow(query, log_id, l.userId, l.medicationId, t_at, l.scheduledFor)
    return {"status": "success", "id": str(row["id"]) if row else log_id, "medicationContract": 2}


@router.get("/api/medications/{user_id}")
async def get_medications(user_id: str, uid: str = Depends(firebase_uid)):
    require_owner(uid, user_id)
    db_pool = _state.db_pool
    if not db_pool:
        return []
    query = "SELECT * FROM medications WHERE user_id = $1 ORDER BY created_at DESC;"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, user_id)
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            if d.get("updated_at"):
                d["updated_at"] = d["updated_at"].isoformat()
            result.append(d)
        return result
 
 
@router.get("/api/medications/{user_id}/logs")
@router.get("/api/sync/medication-logs/{user_id}")
async def get_medication_logs(
    user_id: str,
    days: int = 7,
    uid: str = Depends(firebase_uid),
):
    require_owner(uid, user_id)
    db_pool = _state.db_pool
    if not db_pool:
        return []
    query = """
        SELECT id, user_id, medication_id, taken_at, scheduled_for, actor_id, units_taken, idempotency_key
        FROM medication_logs
        WHERE user_id = $1 AND taken_at >= NOW() - make_interval(days => $2)
        ORDER BY taken_at DESC;
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, user_id, days)
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["medication_id"] = str(d["medication_id"])
            if d.get("taken_at"):
                d["taken_at"] = d["taken_at"].isoformat()
            result.append(d)
        return result


# =============================================================
# LIGA / AUDITORIA / AGENTE
# =============================================================
@router.post("/api/sync/league-membership")
async def sync_league_membership(m: LeagueMembershipSyncDto, uid: str = Depends(firebase_uid)):
    require_owner(uid, m.userId)
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "LEAGUE_FEATURE_DISABLED",
            "message": "Liga Semanal desativada. O histórico foi preservado sem novas gravações.",
        },
    )


@router.post("/api/sync/audit-log")
async def sync_audit_log(a: AuditLogSyncDto, uid: str = Depends(firebase_uid)):
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode"}
    query = """
    INSERT INTO audit_logs (id, user_id, event_type, action, metadata, created_at)
    VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
    ON CONFLICT (id) DO NOTHING
    RETURNING id;
    """
    log_id = a.id or str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        if uid != a.userId:
            await require_family_access(conn, uid, a.userId)
        row = await conn.fetchrow(query, log_id, a.userId, a.eventType, a.action, a.metadataJson)
    return {"status": "success", "id": str(row["id"]) if row else log_id}


@router.post("/api/agent/receipt")
async def agent_receipt(r: AgentReceiptDto, uid: str = Depends(firebase_uid)):
    from orb_actions import ACTION_CATALOG, closing_fala
    require_owner(uid, r.userId)
    if r.action not in ACTION_CATALOG:
        raise HTTPException(422, "Ação desconhecida no contrato.")
    if r.status not in ("confirmed", "failed"):
        raise HTTPException(422, "Status inválido.")
    closing = closing_fala(r.action, r.params)
    db_pool = _state.db_pool
    if not db_pool:
        return {"status": "offline_mode", "closing": closing}
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO agent_actions (user_id, channel, action, params, idempotency_key, status, result)
            VALUES ($1, 'app', $2, $3, $4, $5, $6)
            ON CONFLICT (user_id, idempotency_key) DO NOTHING
            RETURNING id;
            """,
            r.userId, r.action, json.dumps(r.params), r.idempotencyKey,
            r.status, json.dumps(r.result or {}))
    try:
        import asyncio
        await asyncio.to_thread(_state.log_fine_tuning_sample, user_prompt=f"[receipt:{r.action}]",
            assistant_reply=closing, source="agent_receipt",
            system_prompt="recibo de executor (contrato B0)",
            action=r.action, action_params=r.params)
    except Exception:
        pass
    return {"status": "success" if row else "duplicate", "closing": closing}


class _AppState:
    """Porta das dependencias do monolito (db_pool, rate_limiter,
    dispatch_clinical_alert, vinculo TOTP, dataset). Preenchida por
    server.py no startup."""

    db_pool = None
    rate_limiter = None
    dispatch_clinical_alert = None
    log_fine_tuning_sample = None


_state = _AppState()


def configure_state(*, db_pool=None, rate_limiter=None, dispatch_clinical_alert=None,
                    log_fine_tuning_sample=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.rate_limiter = rate_limiter
    _state.dispatch_clinical_alert = dispatch_clinical_alert
    _state.log_fine_tuning_sample = log_fine_tuning_sample
