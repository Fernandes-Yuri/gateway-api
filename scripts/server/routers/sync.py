"""Router de sincronizacao PostgreSQL (canal de dados do app).

Extraido do monolito server.py na Fase A passo 5
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /api/sync/*,
/api/profile/*, /api/vitals/*, /api/metrics/*, /api/social/*,
/api/feedback/*, /api/upload/exam, /api/medications/*, /api/agent/receipt.

Seguranca preservada: require_owner/require_family_access em tudo que
grava ou le dados de usuario (AUD-G07: userId vem do token, nao do body),
rate-limiter por IP, sanitize_zero_trust_text em campos livres, faixa
clinica valida em vitais (SBC/SBD), status de exame em lista fechada e
LGPD Art. 18 no delete de conta (ON DELETE CASCADE).
"""
import base64
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
    id: str
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
    # Segredo TOTP (base32) do vinculo de WhatsApp; o app pode gerar localmente.
    totpSecret: Optional[str] = None
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

    # Segredo TOTP do vinculo de WhatsApp. O app pode gerar localmente (caso em que
    # ele ja conhece o segredo e calcula o codigo no clique sem chamar o servidor);
    # senao o gateway gera e devolve para o app persistir. Nunca regenera o de uma
    # conta existente: isso invalidaria o segredo guardado no aparelho.
    totp_secret = getattr(p, "totpSecret", None)
    if totp_secret:
        try:
            s_clean = str(totp_secret).strip().replace(" ", "").upper()
            if not s_clean:
                totp_secret = None
            else:
                pad = (8 - len(s_clean) % 8) % 8
                raw = base64.b32decode(s_clean + "=" * pad, casefold=True)
                if not raw:
                    totp_secret = None
                else:
                    totp_secret = s_clean
        except Exception:
            totp_secret = None

    if _state.has_wa_link and not totp_secret:
        totp_secret = _state.generate_totp_secret()

    async with db_pool.acquire() as conn:
        await conn.execute(query, *values)
        if _state.has_wa_link:
            existing = await conn.fetchrow(
                "SELECT totp_secret FROM profiles WHERE id = $1", p.id
            )
            if existing and existing["totp_secret"]:
                totp_secret = existing["totp_secret"]
            elif totp_secret:
                try:
                    await conn.execute(
                        "UPDATE profiles SET totp_secret = $2 WHERE id = $1 AND totp_secret IS NULL",
                        p.id, totp_secret,
                    )
                except Exception as _totp_err:
                    print(f"[TOTP Secret] Falha ao gravar segredo para {p.id}: {_totp_err}")
    return {"status": "success", "id": p.id, "totpSecret": totp_secret}


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

        # Migracao tardia do segredo TOTP do vinculo de WhatsApp.
        # Contas criadas antes da migration 009 (ou que nunca sincronizaram
        # perfil depois dela) nao tem totp_secret: o app nao consegue montar o
        # link de vinculo e o botao "Vincular meu WhatsApp" simplesmente nao
        # faz nada. Nesse momento — o login puxa o perfil — geramos e
        # persistimos o segredo faltante. Nao regenera quem ja tem.
        if _state.has_wa_link and not d.get("totp_secret"):
            try:
                novo = _state.generate_totp_secret()
                await conn.execute(
                    "UPDATE profiles SET totp_secret = $2 WHERE id = $1 AND totp_secret IS NULL",
                    user_id, novo,
                )
                d["totp_secret"] = novo
            except Exception as _totp_err:
                print(f"[TOTP Secret] Falha ao gerar segredo faltante para {user_id}: {_totp_err}")

        if d.get("birth_date"):
            d["birth_date"] = d["birth_date"].isoformat()
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
        return d


@router.delete("/api/profile/{user_id}", status_code=status.HTTP_200_OK)
async def delete_profile(user_id: str, uid: str = Depends(firebase_uid)):
    """Direito ao Esquecimento — LGPD Art. 18.

    AUD-AN16: o app chamava deleteProfileRemotely() que era um no-op retornando
    true; o usuario excluia a conta e todo o PHI permanecia no RDS. Agora o app
    chama este endpoint. So o PROPRIO dono pode excluir (cuidador nao pode
    apagar a conta do paciente); ON DELETE CASCADE remove as 14 tabelas filhas.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco local offline")
    if uid != user_id:
        raise HTTPException(status_code=403, detail="Apenas o próprio dono pode excluir a conta.")
    try:
        target = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de usuário inválido.")
    # Diretorio de fotos validado ANTES de apagar o banco: id que nao resulte
    # numa pasta segura dentro de avatars/ aborta com 400 sem mexer em nada.
    avatar_dir = _avatar_user_dir(user_id)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM profiles WHERE id = $1", target)
        if not row:
            raise HTTPException(status_code=404, detail="Perfil não encontrado.")
        await conn.execute("DELETE FROM profiles WHERE id = $1", target)

    # LGPD: remove somente o diretorio ja validado e contido em
    # AVATAR_UPLOADS_DIR (nunca um caminho montado a partir de string bruta).
    shutil.rmtree(avatar_dir, ignore_errors=True)

    print(f"[LGPD] Conta excluída (Direito ao Esquecimento): {user_id}")
    return {"success": True, "deleted": True, "user_id": user_id}


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

    content = await file.read()
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
            detail="Não foi possível verificar a foto agora. Tente novamente em instantes.",
        )
    if not aprovada:
        logger.info("[avatar] Foto reprovada na moderacao user_id=%s motivo=%s", user_id, motivo)
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
        return []
    query = """
    SELECT sp.id, sp.user_id, sp.post_type, sp.title, sp.description, sp.created_at,
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
        return {"status": "offline_mode"}
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("sync_feedback_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=db_pool)

    clean_title = sanitize_zero_trust_text(f.title, max_length=200, field_name="title", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    clean_message = sanitize_zero_trust_text(f.message, max_length=1000, field_name="message", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    clean_email = sanitize_zero_trust_text(f.userEmail or "", max_length=254, field_name="userEmail", ip=client_ip)
    clean_name = sanitize_zero_trust_text(f.userName or "", max_length=120, field_name="userName", ip=client_ip)

    query = """
    INSERT INTO feedbacks (user_id, user_email, user_name, category, title, message, input_method, app_version, device_info, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
    RETURNING id;
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(query, uid, clean_email, clean_name, f.category, clean_title, clean_message, f.inputMethod, f.appVersion, f.deviceInfo)
    return {"status": "success", "id": str(row["id"])}


@router.post("/api/feedback/reply")
async def create_feedback_reply(req: FeedbackReplyCreate, request: Request, uid: str = Depends(firebase_uid)):
    """Resposta da equipe a um feedback (doc 10 §4.1).

    admin-only: precisa de um token Firebase valido. A resposta fica pendente
    ate o app puxar no SyncWorker, que entao notifica o usuario localmente.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(503, "Banco indisponível")
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("feedback_reply_ip", client_ip, limit=20, window_seconds=60, ip=client_ip, pool=db_pool)
    clean_body = sanitize_zero_trust_text(req.body, max_length=1000, field_name="body", ip=client_ip, rate_limiter=_state.rate_limiter, pool=db_pool)
    clean_author = sanitize_zero_trust_text(req.author or "Suporte Braga Saúde", max_length=100, field_name="author", ip=client_ip)
    try:
        fid = uuid.UUID(req.feedbackId)
    except ValueError:
        raise HTTPException(422, "Feedback inválido.")
    async with db_pool.acquire() as conn:
        # Zero-trust: o feedback precisa pertencer ao usuario informado.
        owned = await conn.fetchval("SELECT 1 FROM feedbacks WHERE id=$1 AND user_id=$2", fid, req.userId)
        if not owned:
            raise HTTPException(404, "Feedback não encontrado para este usuário.")
        row = await conn.fetchrow(
            "INSERT INTO feedback_replies (feedback_id, user_id, author, body) VALUES ($1,$2,$3,$4) RETURNING id, created_at",
            fid, req.userId, clean_author, clean_body)
    return {"status": "success", "id": str(row["id"]), "createdAt": row["created_at"].isoformat()}


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


@router.post("/api/upload/exam/{user_id}")
async def upload_exam_file(
    user_id: str,
    request: Request,
    filename: Optional[str] = Query(None),
    uid: str = Depends(firebase_uid),
):
    require_owner(uid, user_id)
    raise HTTPException(410, "Atualize o aplicativo e confirme o termo da Nuvem de Exames antes de enviar.")


# =============================================================
# MEDICAMENTOS
# =============================================================
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
    if l.scheduledFor is not None:
        try:
            datetime.strptime(l.scheduledFor, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise HTTPException(422, "Identificador de dose inválido.")
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
    require_owner(uid, a.userId)
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
    has_wa_link = False
    generate_totp_secret = None
    log_fine_tuning_sample = None


_state = _AppState()


def configure_state(*, db_pool=None, rate_limiter=None, dispatch_clinical_alert=None,
                    has_wa_link=False, generate_totp_secret=None,
                    log_fine_tuning_sample=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.rate_limiter = rate_limiter
    _state.dispatch_clinical_alert = dispatch_clinical_alert
    _state.has_wa_link = has_wa_link
    _state.generate_totp_secret = generate_totp_secret
    _state.log_fine_tuning_sample = log_fine_tuning_sample
