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
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api_identity import firebase_uid, require_owner, require_family_access
from config import STORAGE_EXAMS_DIR, PUBLIC_API_BASE_URL
from profile_contract import profile_upsert
from security_defense import get_client_ip, sanitize_zero_trust_text

router = APIRouter()


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
    if _state.has_wa_link and not totp_secret:
        totp_secret = _state.generate_totp_secret()

    async with db_pool.acquire() as conn:
        await conn.execute(query, *values)
        if _state.has_wa_link and totp_secret:
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

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM profiles WHERE id = $1", target)
        if not row:
            raise HTTPException(status_code=404, detail="Perfil não encontrado.")
        await conn.execute("DELETE FROM profiles WHERE id = $1", target)

    print(f"[LGPD] Conta excluída (Direito ao Esquecimento): {user_id}")
    return {"success": True, "deleted": True, "user_id": user_id}


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
    INSERT INTO social_posts (user_id, post_type, title, description, created_at)
    VALUES ($1, $2, $3, $4, $5)
    RETURNING id;
    """
    try:
        c_at = datetime.fromisoformat(p.createdAt.replace("Z", "+00:00"))
    except Exception:
        c_at = datetime.utcnow()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(query, p.userId, p.postType, p.title, p.description, c_at)
    return {"status": "success", "id": str(row["id"])}


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
           p.full_name as user_name, p.avatar_identifier,
           (SELECT COUNT(*) FROM post_reactions pr WHERE pr.post_id = sp.id) as reaction_count
    FROM social_posts sp
    JOIN profiles p ON sp.user_id = p.id
    WHERE sp.is_visible = TRUE
    ORDER BY sp.created_at DESC
    LIMIT $1 OFFSET $2;
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, limit, offset)
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["created_at"] = d["created_at"].isoformat()
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
    """
    Recebe o arquivo PDF do exame diretamente no armazenamento seguro do servidor.
    Elimina 100% qualquer dependencia de Firebase Cloud Storage e mantem os dados privados.
    """
    # Zero-trust: upload anonimo permitia gravar arquivos no storage de
    # qualquer usuario (hosting de arquivos maliciosos + exaustao de disco).
    require_owner(uid, user_id)
    safe_user_id = re.sub(r'[^a-zA-Z0-9_-]', '', user_id)
    if not safe_user_id:
        raise HTTPException(status_code=400, detail="ID de usuário inválido")

    db_pool = _state.db_pool
    client_ip = get_client_ip(request)
    if _state.rate_limiter:
        await _state.rate_limiter.check("upload_exam_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=db_pool)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Arquivo vazio")

    # Limite de tamanho: 20 MB por laudo.
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Arquivo excede o limite de 20 MB.")

    user_dir = os.path.join(STORAGE_EXAMS_DIR, safe_user_id)
    os.makedirs(user_dir, exist_ok=True)

    raw_filename = filename or f"laudo_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    safe_filename = os.path.basename(raw_filename)
    if not safe_filename.lower().endswith(".pdf"):
        safe_filename += ".pdf"

    file_path = os.path.join(user_dir, safe_filename)
    with open(file_path, "wb") as f:
        f.write(body)

    file_url = f"{PUBLIC_API_BASE_URL}/api/files/exams/{safe_user_id}/{safe_filename}"
    print(f"[Storage] Laudo salvo com sucesso: {file_path} ({len(body)} bytes) para usuário {safe_user_id}")
    return {
        "status": "success",
        "fileUrl": file_url,
        "fileName": safe_filename,
        "sizeBytes": len(body)
    }


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
