import os
import re
import json
import hashlib
import asyncio
import secrets
import random
import uuid
import time
# AUD-G(B): deque/Deque e RedirectResponse mortos — so o import, sem uso.
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from profile_contract import profile_upsert
from orb_router import route_chat, detect_all, recall, remember, resolve_followup, whatsapp_next, dedupe_fala, symptom_repeat, SYMPTOM_KINDS, ESCALATION_SUFFIX

# Vínculo de WhatsApp por TOTP (arquitetura temporária, até a Meta liberar template).
try:
    from wa_link_totp import generate_totp_secret, current_totp_code
    from wa_link_service import handle_link_attempt
    from wa_gate_service import gate_unregistered, sender_has_linked_account
    HAS_WA_LINK = True
    HAS_WA_GATE = True
except ImportError:
    HAS_WA_LINK = False
    HAS_WA_GATE = False

    def generate_totp_secret(*args, **kwargs):  # type: ignore
        return None

    def current_totp_code(*args, **kwargs):  # type: ignore
        return None

    async def handle_link_attempt(*args, **kwargs):  # type: ignore
        return False

    async def gate_unregistered(*args, **kwargs):  # type: ignore
        return False

    async def sender_has_linked_account(*args, **kwargs):  # type: ignore
        return None
from trace_llm import trace_llm
from api_identity import firebase_uid, require_owner, require_family_access
from voice_scheduling import quer_agendar, agendar_por_voz
from assistant_preferences import SCHEMA as PREFERENCE_SCHEMA, install_preference_routes, process_preferences
from piper_service import PiperService
from conversation_policy import build_conversation_system
from family_api import install_family_routes, purge_expired_family_messages
from caregiver_alerts import install_caregiver_alert_routes
from vip_api import install_vip_routes
from exams_api import install_exam_routes
from security_defense import (
    StrictRateLimiter,
    get_client_ip,
    is_prompt_injection,
    sanitize_zero_trust_text,
    INJECTION_PATTERNS,
)
import requests
import httpx

# Nucleo extraido (Fase A passo 1): sanitizacao de TTS e PII (LGPD).
from core.text import clean_text_for_tts, sanitize_pii, EMOJI_REGEX, PHONE_REGEX, CPF_REGEX, EMAIL_REGEX, CEP_REGEX, OTP_REGEX
# Nucleo extraido (Fase A passo 2): helpers de dispositivos e FCM.
from core.devices import load_devices, save_devices, dispatch_push
# Routers extraidos (Fase A passo 2): health, dataset, devices, notify, emergency.
from routers import health as health_router
# Routers extraidos (Fase A passo 3): app/OTA/landing + audio/TTS.
from routers import app as app_router
from routers import audio as audio_router
# Routers extraidos (Fase A passo 4): chat conversacional.
from routers import chat as chat_router
# Routers extraidos (Fase A passo 4b): autenticacao (OTP, reset, conta).
from routers import auth as auth_router
# Routers extraidos (Fase A passo 5): sincronizacao PostgreSQL.
from routers import sync as sync_router
# Modulos core extraidos (Fase A passo 6): confirmacao de leituras vitais
# (maquina de pendencias + validacao clinica + botes do WhatsApp) e
# curadoria do dataset de fine-tuning.
import core.confirmation as confirmation_module
from core.confirmation import (
    HEALTH_VALIDATION_RULES,
    validate_health_data,
    generate_confirmation_code,
    create_pending_confirmation,
    confirm_pending_confirmation,
    cancel_pending_confirmation,
    build_confirmation_message,
    send_confirmation_buttons,
)
import core.dataset as dataset_module
from core.dataset import (
    detect_clinical_category,
    log_fine_tuning_sample,
)


# Configuração central (Fase B, passo 1 — doc 36): env vars, defaults e paths.
from config import (
    APP_DIR, PROJECT_ROOT, USES_ORGANIZED_LAYOUT,
    DATASET_PATH, AUDIO_CACHE_DIR, STORAGE_DIR, STORAGE_EXAMS_DIR, DEVICES_PATH, RELEASES_DIR,
    DATABASE_URL,
    GROQ_API_KEY, GROQ_API_URL, MODEL_ID, MODEL_CHAT, MODEL_REST, MODEL_WHATSAPP,
    GROQ_API_KEY_CHAT, GROQ_API_KEY_WHATSAPP,
    PIPER_BIN, PIPER_MODELS_DIR,
    SERVICE_ACCOUNT_PATH,
    PUBLIC_API_BASE_URL,
)

# Suporte defensivo a E-mail de Segurança & OTP
try:
    from email_service import is_email_configured, send_security_code_email, send_welcome_email, send_security_alert_email
    HAS_EMAIL = True
except ImportError:
    HAS_EMAIL = False

# Validacao server-side do Google reCAPTCHA (zero-trust no auth)
try:
    from recaptcha_verify import verify_recaptcha
    HAS_RECAPTCHA = True
except ImportError:
    HAS_RECAPTCHA = False

    def verify_recaptcha(*args, **kwargs):  # type: ignore
        return {"valid": False, "reason": "module_unavailable"}

# Verificacao de assinatura HMAC do webhook da Meta (zero-trust no webhook)
try:
    from webhook_security import verify_meta_signature
    HAS_WEBHOOK_SECURITY = True
except ImportError:
    HAS_WEBHOOK_SECURITY = False

    def verify_meta_signature(*args, **kwargs):  # type: ignore
        return False

# Suporte defensivo a WhatsApp Business Cloud API
try:
    from whatsapp_service import (
        send_whatsapp_text,
        send_whatsapp_template,
        send_clinical_alert_whatsapp,
        send_security_code_whatsapp,
        download_whatsapp_media,
        transcribe_audio_whisper,
        sanitize_phone_number,
        mark_whatsapp_message_read,
        send_whatsapp_reaction,
        send_whatsapp_audio,
        send_whatsapp_interactive,
        send_whatsapp_list,
        upload_whatsapp_media,
        is_otp_request,
        phones_match,
        build_wa_link,
        WHATSAPP_PHONE_NUMBER_ID
    )
    HAS_WHATSAPP = True
except ImportError:
    HAS_WHATSAPP = False

# ---------------------------------------------------------------------------
# PAUSA TEMPORARIA DO CANAL WHATSAPP PARA OTP (18/09/2026)
# ---------------------------------------------------------------------------
# A Meta nao aprova os templates de autenticacao e a conta business ainda nao
# foi verificada, entao o codigo OTP por WhatsApp nunca chegava ao usuario.
# O app agora usa SOMENTE e-mail para OTP. O canal WhatsApp foi pausado aqui
# (nao removido): o codigo continua intacto para religar assim que a Meta
# liberar o template.
#
# Para religar: basta colocar WHATSAPP_OTP_PAUSED = False.
# ---------------------------------------------------------------------------
WHATSAPP_OTP_PAUSED = True

# Suporte defensivo a asyncpg para PostgreSQL
try:
    import asyncpg
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# Suporte defensivo a Agendador em Segundo Plano (APScheduler)
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

# Suporte defensivo a Firebase Cloud Messaging (FCM)
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    if SERVICE_ACCOUNT_PATH and os.path.exists(SERVICE_ACCOUNT_PATH):
        if not firebase_admin._apps:
            cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
            firebase_admin.initialize_app(cred)
        HAS_FCM = True
    else:
        HAS_FCM = False
except Exception:
    HAS_FCM = False

# GROQ/PIPER/PUBLIC_API_BASE_URL vindos de `config` (Fase B, passo 1).
# PIPER_BIN/PIPER_MODELS_DIR podem ser None se o .env nao os definir; o
# PiperService entende isso e a sintese simplesmente falha com aviso
# (config.py ja avisou no startup).
PIPER_SERVICE = PiperService(PIPER_BIN, PIPER_MODELS_DIR)
GROQ_SEMAPHORE = asyncio.Semaphore(5)
# Concorrência por canal (isolamento: um canal lento não mata os outros).
SEM_CHAT = asyncio.Semaphore(3)
SEM_REST = asyncio.Semaphore(2)
SEM_WA = asyncio.Semaphore(3)

app = FastAPI(title="Braga Saúde Gateway, TTS, Sync & Notification Hub", version="5.0")

# Routers extraidos (Fase A passo 2): o estado global fica aqui no server e e
# passado para o router via configure_state; os endpoints leem sem acoplar.
health_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    has_postgres=HAS_POSTGRES,
    has_scheduler=HAS_SCHEDULER,
    has_fcm=HAS_FCM,
    has_whatsapp=HAS_WHATSAPP,
    messaging=messaging if HAS_FCM else None,
)
app.include_router(health_router.router)
app_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    messaging=messaging if HAS_FCM else None,
    has_fcm=HAS_FCM,
)
app.include_router(app_router.router)
audio_router.configure_state(piper_service=PIPER_SERVICE)
app.include_router(audio_router.router)

# Origens permitidas via env (separadas por vírgula). "CORS *" com credenciais
# é inseguro e inválido perante a spec; a lista precisa ser explícita.
# A PWA cuidador e servida em braga-saude.web.app E bragasaude.online/app/
# (README do site-pwa) -- os dois precisam estar aqui, senao o navegador
# bloqueia todas as chamadas e o app para de funcionar.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://bragasaude.online,https://www.bragasaude.online,"
        "https://braga-saude.web.app,https://braga-saude.firebaseapp.com,"
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.getenv(
    "BRAGA_PROJECT_ROOT",
    os.path.dirname(APP_DIR) if os.path.basename(APP_DIR) == "app" else APP_DIR,
)
USES_ORGANIZED_LAYOUT = os.path.basename(APP_DIR) == "app"

if USES_ORGANIZED_LAYOUT:
    DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")
    DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
    default_dataset_path = os.path.join(DEFAULT_DATA_DIR, "dataset", "dataset_braga.jsonl")
    default_audio_cache_dir = os.path.join(DEFAULT_CACHE_DIR, "audio")
    default_storage_dir = os.path.join(DEFAULT_DATA_DIR, "storage")
    default_devices_path = os.path.join(DEFAULT_DATA_DIR, "devices", "registered_devices.json")
else:
    # Mantém o comportamento atual para execução local fora da pasta app/.
    default_dataset_path = os.path.join(PROJECT_ROOT, "dataset_braga.jsonl")
    default_audio_cache_dir = os.path.join(PROJECT_ROOT, "audio_cache")
    default_storage_dir = os.path.join(PROJECT_ROOT, "storage")
    default_devices_path = os.path.join(PROJECT_ROOT, "registered_devices.json")

DATASET_PATH = os.getenv("DATASET_PATH", default_dataset_path)
AUDIO_CACHE_DIR = os.getenv("AUDIO_CACHE_DIR", default_audio_cache_dir)
STORAGE_DIR = os.getenv("STORAGE_DIR", default_storage_dir)
STORAGE_EXAMS_DIR = os.path.join(STORAGE_DIR, "exams")
DEVICES_PATH = os.getenv("DEVICES_PATH", default_devices_path)
os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
os.makedirs(STORAGE_EXAMS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DEVICES_PATH), exist_ok=True)

db_pool: Optional[Any] = None

async def _vip_notify_admin(text: str):
    """doc 10 §4.4: avisa o admin no WhatsApp a cada inscrição VIP nova."""
    if HAS_WHATSAPP and 'send_whatsapp_text' in globals():
        admin_phone = (os.getenv('ADMIN_PHONE')
                       or os.getenv('SECURITY_ALERT_PHONE')
                       or '5511967808252')
        await send_whatsapp_text(admin_phone, text)

async def _vip_confirm_email(to_email: str, user_name: str):
    """doc 10 §4.4: confirma a inscrição VIP por e-mail (best-effort)."""
    if HAS_EMAIL and 'send_welcome_email' in globals():
        await send_welcome_email(to_email, user_name)

# doc 10 §4.4: inscrição VIP avisa o admin (WhatsApp) e confirma por e-mail ao inscrito.
install_vip_routes(
    app, lambda: db_pool,
    notify_admin=_vip_notify_admin,
    send_confirmation_email=_vip_confirm_email,
)
install_preference_routes(app, lambda: db_pool)
WHATSAPP_EVENT_LOG: List[Dict[str, Any]] = []

# AUD-G(M): outbox durável do webhook. Antes o handler ackava 200 para a Meta
# e só DEPOIS processava numa BackgroundTask — se o processo caísse nesse meio,
# a mensagem era perdida para sempre (a Meta não reenvia quem já recebeu 200).
# O WHATSAPP_EVENT_LOG é só diagnóstico e vive em memória. Este arquivo JSONL é
# o registro de recuperação: escrito ANTES do ack, sobrevive a crash/restart.
WEBHOOK_OUTBOX_PATH = os.path.join(PROJECT_ROOT, "webhook_outbox.log")


rate_limiter = StrictRateLimiter()

@app.middleware("http")
async def security_firewall_middleware(request: Request, call_next):
    """Firewall de seguranca Zero-Trust no nivel da Gateway.
    Bloqueia sumariamente por 24h qualquer IP que tenha acumulado 3 violacoes.
    """
    client_ip = get_client_ip(request)
    blocked, remaining = rate_limiter.is_ip_blocked(client_ip)
    if blocked:
        # Permite healthcheck do orquestrador
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={
                "error": "IP_BLOCKED_24H",
                "detail": "Acesso bloqueado por 24 horas para este endereco IP devido a excesso de tentativas e violacoes de seguranca.",
                "retry_after_seconds": remaining,
            },
            headers={
                "Retry-After": str(remaining),
                "X-RateLimit-Blocked": "true",
            },
        )
    return await call_next(request)

# =============================================================
# MODELOS PYDANTIC
# =============================================================
# Message e ChatCompletionRequest: movidos para routers/chat.py (Fase A passo 4).

# VoiceRequest: movido para routers/audio.py (Fase A passo 3).

# DeviceRegistrationRequest, AlertNotificationRequest, EmergencyTriggerRequest:
# movidos para routers/health.py (Fase A passo 2).

class WhatsAppSendRequest(BaseModel):
    phone: str
    message: Optional[str] = None
    templateName: Optional[str] = None
    languageCode: Optional[str] = "en_US"

# Modelos de auth (OtpSend, OtpVerify, PasswordReset, WelcomeEmail,
# CheckAccount): movidos para routers/auth.py (Fase A passo 4b).

# Modelos de Sincronização PostgreSQL
# DTOs de sync (Profile, VitalSign, Medication*, DailyMetric, SocialPost,
# PostReaction, Feedback, Exam*, AuditLog, LeagueMembership, FeedbackReplyCreate,
# AgentReceiptDto): movidos para routers/sync.py (Fase A passo 5).

class FamilyBindingSyncDto(BaseModel):
    id: Optional[str] = None
    patientId: str
    caregiverId: Optional[str] = None
    caregiverName: str
    caregiverRelation: str
    connectionCode: str
    status: Optional[str] = "PENDING"
    createdAt: str
    expiresAt: str

class FamilyMessageSyncDto(BaseModel):
    id: Optional[str] = None
    patientId: str
    senderId: str
    senderName: str
    messageText: str
    iconType: Optional[str] = "LOVE"
    isRead: Optional[bool] = False
    sentAt: str

# =============================================================
# INICIALIZAÇÃO DE POOL E SCHEDULER
# =============================================================
@app.on_event("startup")
async def startup():
    global db_pool
    if HAS_POSTGRES:
        for attempt in range(1, 6):
            try:
                db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, timeout=10)
                print(f"[PostgreSQL] Pool de conexões assíncrono estabelecido com sucesso na tentativa {attempt}!")
                async with db_pool.acquire() as conn:
                    await conn.execute(PREFERENCE_SCHEMA)
                    await conn.execute("""
                    CREATE TABLE IF NOT EXISTS auth_verification_codes (
                        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                        identifier VARCHAR(255) NOT NULL,
                        code VARCHAR(10) NOT NULL,
                        purpose VARCHAR(50) NOT NULL,
                        channel VARCHAR(20) NOT NULL,
                        attempts INT DEFAULT 0,
                        is_used BOOLEAN DEFAULT FALSE,
                        reset_token VARCHAR(255),
                        expires_at TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_auth_codes_lookup ON auth_verification_codes(identifier, purpose, is_used);
                    CREATE INDEX IF NOT EXISTS idx_auth_codes_token ON auth_verification_codes(reset_token);
                    """)
                    print("[PostgreSQL] Tabela auth_verification_codes inicializada com sucesso!")
                    await conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id           VARCHAR(64)  PRIMARY KEY,
                        user_id      VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        event_type   VARCHAR(100) NOT NULL,
                        action       VARCHAR(100) NOT NULL,
                        metadata     JSONB,
                        created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id, created_at DESC);
                    """)
                    print("[PostgreSQL] Tabela audit_logs inicializada com sucesso!")
                    await conn.execute("""
                    CREATE TABLE IF NOT EXISTS agent_actions (
                        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                        user_id         VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        channel         VARCHAR(20) NOT NULL,
                        action          VARCHAR(50) NOT NULL,
                        params          JSONB NOT NULL DEFAULT '{}',
                        idempotency_key VARCHAR(64) NOT NULL,
                        status          VARCHAR(20) NOT NULL DEFAULT 'proposed',
                        result          JSONB NOT NULL DEFAULT '{}',
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (user_id, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_actions_user ON agent_actions(user_id, created_at DESC);
                    """)
                    print("[PostgreSQL] Tabela agent_actions inicializada com sucesso!")
                    await conn.execute("""
                    CREATE TABLE IF NOT EXISTS security_blocked_ips (
                        ip VARCHAR(45) PRIMARY KEY,
                        reason TEXT NOT NULL,
                        violations INT NOT NULL DEFAULT 1,
                        blocked_until TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_security_blocked_ips_until ON security_blocked_ips(blocked_until);
                    """)
                    print("[PostgreSQL] Tabela security_blocked_ips inicializada com sucesso!")
                break
            except Exception as e:
                print(f"[PostgreSQL Error] Tentativa {attempt}/5 falhou ao conectar ao banco: {e}")
                if attempt < 5:
                    await asyncio.sleep(3)

        if db_pool:
            await rate_limiter.load_blocked_ips_from_db(db_pool)

            # AUD-G(M): expire_pending_confirmations() (migration 004:63-71)
            # existia mas nunca era chamada. Confirmações vencidas ficavam
            # status='pending' para sempre — índice parcial
            # idx_pending_conf_expires e o relatório v_confirmation_report
            # acumulavam lixo. Expira a cada boot do serviço.
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute("SELECT expire_pending_confirmations()")
            except Exception as e:
                print(f"[Startup] Aviso: função expire_pending_confirmations() indisponível: {e}")

        # Inicializa cache assíncrono de áudios engatilhados
        asyncio.create_task(init_fast_lane_audio_cache())

        # Pass the coroutine directly: a sync lambda runs in an executor without an event loop.
        if db_pool and HAS_SCHEDULER:
            await purge_expired_family_messages(db_pool)
            global retention_scheduler
            retention_scheduler = AsyncIOScheduler()
            retention_scheduler.add_job(
                purge_expired_family_messages, 'interval', seconds=30,
                args=[db_pool], id="purge_family_messages_24h",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            async def purge_expired_blocked_ips():
                if db_pool:
                    try:
                        async with db_pool.acquire() as conn:
                            await conn.execute("DELETE FROM security_blocked_ips WHERE blocked_until <= NOW();")
                    except Exception as err:
                        print(f"[SECURITY FIREWALL] Falha ao limpar IPs expirados: {err}")
            retention_scheduler.add_job(
                purge_expired_blocked_ips, 'interval', minutes=5,
                id="purge_security_blocked_ips", replace_existing=True, max_instances=1,
            )
            # D52: retencao de releases de APK (~36MB cada). Mantem so os 3
            # mais recentes; protege o release ativo e o arquivo servido.
            async def purge_old_release_apks():
                try:
                    from release_retention import purge_old_releases
                    res = await purge_old_releases(db_pool, RELEASES_DIR, keep=3)
                    if res["apagados"]:
                        print(f"[RETENTION] {len(res['apagados'])} APK(s) antigo(s) removido(s): {res['apagados']}")
                except Exception as err:
                    print(f"[RETENTION] Falha na limpeza de releases: {err}")
            retention_scheduler.add_job(
                purge_old_release_apks, 'interval', hours=6,
                id="purge_old_release_apks", replace_existing=True, max_instances=1,
                coalesce=True,
            )
            # Limpeza imediata na inicializacao (nao espera 6h para a 1a passada)
            await purge_old_release_apks()

            # doc 10 §4.6: rotinas pró-ativas (09h / 14h30 / 20h30). Antes isto
            # era uma função solta que criava um scheduler aninhado e nunca era
            # registrada — código morto. Agora vive no scheduler existente.
            retention_scheduler.add_job(
                lambda: asyncio.create_task(scheduled_proactive_job("manha")),
                CronTrigger(hour=9, minute=0), id="proactive_manha",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            retention_scheduler.add_job(
                lambda: asyncio.create_task(scheduled_proactive_job("hidratacao")),
                CronTrigger(hour=14, minute=30), id="proactive_hidratacao",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            retention_scheduler.add_job(
                lambda: asyncio.create_task(scheduled_proactive_job("noite")),
                CronTrigger(hour=20, minute=30), id="proactive_noite",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            print("[Scheduler] Rotinas pró-ativas registradas: 09h00, 14h30, 20h30.")
            retention_scheduler.start()

async def scheduled_proactive_job(periodo: str):
    """Rotina pró-ativa periódica (doc 10 §4.6).

    Executada pelo retention_scheduler nos horários 09h00 / 14h30 / 20h30.
    Envia um empurrão carinhoso para quem ainda não registrou algo do dia.
    """
    print(f"[Scheduler] Executando rotina pró-ativa agendada: {periodo}")
    if not db_pool:
        return
    try:
        # devices_json é um arquivo, não uma tabela — a fila de dispositivos
        # vive em load_devices(). Usa ela para saber quem existe.
        devices = load_devices()
        for uid, dev in devices.items():
                if dev.get('role') != 'USER':
                    continue
                async with db_pool.acquire() as conn:
                    today = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM vital_signs WHERE user_id=$1 AND measured_at >= CURRENT_DATE)",
                        uid)
                if today:
                    continue
                name = await _proactive_display_name(uid)
                msg = {
                    'manha': f'Bom dia{name}! Que tal registrar sua pressão agora, logo depois do café?',
                    'hidratacao': f'Boa tarde{name}! Já bebeu um copo d\'água? Vale anotar para fechar o dia bem.',
                    'noite': f'Boa noite{name}! Fechou o dia? Que tal registrar como você está se sentindo?',
                }.get(periodo, 'Hora de cuidar de você.')
                try:
                    await asyncio.to_thread(
                        dispatch_push,
                        token=dev.get('token'),
                        title='Lembrete do Braga Saúde',
                        body=msg,
                        data={'type': 'PROACTIVE_BRAGA', 'period': periodo},
                    )
                except Exception as push_err:
                    print(f"[Scheduler] Falha no push pró-ativo para {uid}: {push_err}")
    except Exception as e:
        print(f"[Scheduler] Erro na rotina pró-ativa {periodo}: {e}")


async def _proactive_display_name(uid: str) -> str:
    """Nome de exibição para a rotina pró-ativa; vazio se indisponível."""
    try:
        async with db_pool.acquire() as conn:
            name = await conn.fetchval("SELECT full_name FROM profiles WHERE id=$1", uid)
        return f', {name.split()[0]}' if name else ''
    except Exception:
        return ''

    # Inicializa cache assíncrono de áudios engatilhados
    asyncio.create_task(init_fast_lane_audio_cache())

@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if globals().get("retention_scheduler"):
        retention_scheduler.shutdown(wait=False)
    if db_pool:
        await db_pool.close()

# =============================================================
# MÉTODOS AUXILIARES DE DISPOSITIVOS E FCM — ver core/devices.py
# (Fase A passo 2). Importados de la; nao redeclarar aqui.
# =============================================================




async def dispatch_clinical_alert(
    pool,
    source_user_id: str,
    systolic: Optional[float] = None,
    diastolic: Optional[float] = None,
    glucose: Optional[float] = None,
    glucose_type: Optional[str] = None,
    background_tasks: Optional[BackgroundTasks] = None,
):
    """Gatilho Clínico Automático para Cuidadores (doc 10 §3.2).

    Extraido de sync_vital_sign para que qualquer canal de entrada (app, bot do
    WhatsApp, voz) aplique a MESMA regra: leitura crítica avisa o cuidador por
    push FCM e WhatsApp. Best-effort: falha de notificação nunca quebra quem gravou.

    Retorna True quando havia condição crítica (mesmo que a notificação falhe).
    """
    is_critical_pressure = systolic is not None and systolic >= 140
    is_critical_glucose = glucose is not None and (glucose < 70 or glucose > 200)
    if not (is_critical_pressure or is_critical_glucose):
        return False

    if is_critical_pressure:
        val_str = f"{systolic:g}/{diastolic:g} mmHg" if diastolic is not None else f"{systolic:g} mmHg"
        alert_type = "Pressão Arterial Elevada"
        msg_body = f"Seu familiar registrou pressão de {val_str}."
    else:
        val_str = f"{glucose:g} mg/dL ({glucose_type or 'Glicose'})"
        alert_type = "Glicemia Fora da Faixa"
        msg_body = f"Seu familiar registrou glicemia de {val_str}."

    # 1. Notificação Push (FCM) para aparelhos de cuidador, exceto o próprio.
    try:
        devices = load_devices()
        for uid, dev in devices.items():
            if dev.get("role") == "CAREGIVER" and uid != source_user_id:
                dispatch_push(
                    dev["token"],
                    "Alerta Clínico Braga Saúde",
                    msg_body,
                    {"type": "ALERT_CRITICAL", "sourceUserId": source_user_id},
                )
    except Exception as e:
        print(f"[Clinical Alert] Falha no push: {e}")

    # 2. Alerta via WhatsApp para o contato de emergência/cuidador.
    if HAS_WHATSAPP:
        try:
            async with pool.acquire() as conn:
                p_row = await conn.fetchrow(
                    "SELECT full_name, emergency_contact_phone, emergency_contact_name FROM profiles WHERE id = $1",
                    source_user_id,
                )
            if p_row and p_row.get("emergency_contact_phone"):
                p_name = p_row.get("full_name") or "Familiar"
                target_phone = p_row.get("emergency_contact_phone")
                if background_tasks is not None:
                    background_tasks.add_task(
                        send_clinical_alert_whatsapp, target_phone, p_name, alert_type, val_str
                    )
                else:
                    asyncio.create_task(
                        send_clinical_alert_whatsapp(target_phone, p_name, alert_type, val_str)
                    )
        except Exception as e:
            print(f"[Clinical Alert] Falha no WhatsApp: {e}")

    return True

install_family_routes(app, lambda: db_pool, FamilyBindingSyncDto, FamilyMessageSyncDto,
                      rate_limiter=rate_limiter, load_devices=load_devices, dispatch_push=dispatch_push)
install_caregiver_alert_routes(app, lambda: db_pool, load_devices, dispatch_push)
install_exam_routes(app, lambda: db_pool, rate_limiter=rate_limiter)

# =============================================================
# ROTAS REST DE SINCRONIZACAO POSTGRESQL — ver routers/sync.py
# (Fase A passo 5). 22 endpoints (/api/sync/*, /api/profile/*,
# /api/vitals/*, /api/metrics/*, /api/social/*, /api/feedback/*,
# /api/upload/exam, /api/medications/*, /api/agent/receipt).
# Nao redeclarar aqui.
# =============================================================

# =============================================================
# ENDPOINTS EXTRAIDOS (Fase A passo 2): /health, /dataset/*,
# /api/device*, /api/notify/alert, /api/emergency/trigger
# agora vivem em routers/health.py. Nao redeclarar aqui.
# =============================================================

# (device/register, devices, notify/alert, emergency/trigger: ver routers/health.py)



# AUD-G27: REMOVIDA a cópia local de INJECTION_PATTERNS e is_prompt_injection.
# Ela sombreava a importação de security_defense.py (linha 56-62) sendo uma
# versão REGREDIDA — 6 padrões contra 7, sem o bloco de TOKENS ESPECIAIS de
# modelo (<|im_start|>, <|im_end|>, [INST], <<SYS>>), sem as variantes em
# inglês (forget/bypass/all) e sem a guarda de tipo. Resultado: as rotas que
# mais usam essa checagem (chat WhatsApp, /v1/chat/completions, Orb) eram as
# MENOS protegidas — a doc 03_PWA...:2.4 prometia bloqueio de <|im_start|>
# que nunca acontecia por aqui. Agora só existe UMA definição, a completa.

def get_friendly_safety_response(text: str) -> dict:
    t = text.lower()
    if any(k in t for k in ["contar", "conte", "até 100", "de 1 a", "livro", "poema gigante", "100 linhas"]):
        return {
            "fala": "Eita! Contar isso tudo vai gastar todo o nosso fôlego! Que tal a gente focar no que realmente importa para a sua saúde e rotina hoje?",
            "acao": "CONVERSA",
            "parametros": {}
        }
    if any(k in t for k in ["receite", "remédio", "dosagem", "prescreva", "qual medicamento"]):
        return {
            "fala": "Olha só, para a sua proteção e cuidado, eu não receito remédios nem defino dosagens. O mais seguro é sempre consultar seu médico de confiança! Como posso te apoiar com suas anotações hoje?",
            "acao": "CONVERSA",
            "parametros": {}
        }
    return {
        "fala": "Olha, eu sou o Braga, focado de coração no seu autocuidado e bem-estar! Minhas funções são dedicadas à sua saúde, então vamos focar na sua rotina saudável?",
        "acao": "CONVERSA",
        "parametros": {}
    }

# AUD-G27: is_prompt_injection local REMOVIDO — usa o importado de
# security_defense.py (7 padrões, com tokens especiais de modelo).

from braga_persona import PERSONA
# SYSTEM_PROMPT: definido em core/prompt.py (Fase A passo 4); persona +
# travas clinicas compartilhadas entre routers.
from core.prompt import SYSTEM_PROMPT


# =============================================================
# SANITIZAÇÃO DE TEXTO: ver core/text.py (Fase A passo 1).
# As funcoes clean_text_for_tts e sanitize_pii foram extraidas para la;
# este arquivo importa ambas de core.text.
# =============================================================
# CONFIRMACAO DE LEITURAS VITAIS + DATASET DE FINE-TUNING:
# ver core/confirmation.py e core/dataset.py (Fase A passo 6).
# Maquina de pendencias (pending_confirmations), validacao clinica
# (SBC/SBD), botes do WhatsApp e curadoria do dataset_braga.jsonl.
# Nao redeclarar aqui.
# =============================================================

from ws_orb import install_orb_routes

ORB_CHAT_PROMPT = SYSTEM_PROMPT + (
    " REGRAS CRÍTICAS DE AÇÃO NO CHAT: "
    "1. O chat é ORIENTADOR e EDUCADOR — NUNCA registra nada. PROIBIDO usar 'REGISTRAR_PRESSAO', 'REGISTRAR_GLICEMIA', 'REGISTRAR_AGUA', 'REGISTRAR_BATIMENTOS', 'REGISTRAR_OXIGENACAO', 'BUSCAR_ALIMENTO', 'ABRIR_LISTA_COMPRAS' ou 'CONSULTAR_METRICAS'. "
    "O usuário faz as próprias anotações nas telas do aplicativo; o assistente de voz atua como agente e ajuda a preencher. "
    "2. Ações permitidas no chat: 'CONVERSA' (parametros {}) para tudo; 'EMERGENCIA' só em sintomas graves; 'LEMBRETES' e 'EXAMES' só quando o usuário pedir explicitamente remédios ou exames. "
    "3. NUNCA invente números de saúde nem afirme que salvou ou abriu tela sem confirmação. "
    "4. ANTI-FANTASMA DE DIETA: frases cotidianas sem menção explícita a comida ('tô na correria', 'vou descer ali rapidinho', 'bom dia') são CONVERSA — NUNCA interprete como refeição, NUNCA palpite o que a pessoa comeu, NUNCA dê dica de dieta não solicitada. Só fale de alimento se o usuário citar comida explicitamente ou pedir orientação nutricional. "
)

async def assistant_memory(uid, text, messages):
    return await process_preferences(db_pool, uid, text, messages, url=GROQ_API_URL,
                                     token=GROQ_API_KEY_CHAT, model=MODEL_ID)


install_orb_routes(
    app, pool_provider=lambda: db_pool, system_prompt=ORB_CHAT_PROMPT,
    model=MODEL_ID, url=GROQ_API_URL, token=GROQ_API_KEY_CHAT, memory_handler=assistant_memory,
    semaphore=SEM_CHAT,
    injection_check=is_prompt_injection, log_sample=log_fine_tuning_sample,
    first_timeout=15.0, chunk_timeout=25.0,
)

# Registro do router de chat (Fase A passo 4): feito AQUI, depois de
# assistant_memory/ORB_CHAT_PROMPT/install_orb_routes estarem definidos.
chat_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    groq_api_key=GROQ_API_KEY_CHAT,
    groq_api_url=GROQ_API_URL,
    model_rest=MODEL_REST,
    sem_rest=SEM_REST,
    assistant_memory=assistant_memory,
    get_friendly_safety_response=get_friendly_safety_response,
    log_fine_tuning_sample=log_fine_tuning_sample,
)
app.include_router(chat_router.router)

# Registro do router de auth (Fase A passo 4b): os modulos defensivos
# (email_service, whatsapp_service, recaptcha) sao importados no topo deste
# arquivo com fallback gracioso; passamos o que estiver disponivel.
auth_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    rate_limiter=rate_limiter,
    whatsapp_otp_paused=WHATSAPP_OTP_PAUSED,
    has_email=HAS_EMAIL,
    has_whatsapp=HAS_WHATSAPP,
    has_fcm=HAS_FCM,
    firebase_admin=firebase_admin if HAS_FCM else None,
    is_email_configured=is_email_configured if HAS_EMAIL else None,
    send_security_code_email=send_security_code_email if HAS_EMAIL else None,
    send_security_code_whatsapp=send_security_code_whatsapp if HAS_WHATSAPP else None,
    send_security_alert_email=send_security_alert_email if HAS_EMAIL else None,
    send_welcome_email=send_welcome_email if HAS_EMAIL else None,
    build_wa_link=build_wa_link if HAS_WHATSAPP else None,
    verify_recaptcha=verify_recaptcha if HAS_RECAPTCHA else None,
)
app.include_router(auth_router.router)

# Registro do router de sync (Fase A passo 5): 22 endpoints de sincronizacao
# PostgreSQL. dispatch_clinical_alert e log_fine_tuning_sample continuam aqui
# (compartilhados com o WhatsApp); passados por configure_state.
sync_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    rate_limiter=rate_limiter,
    dispatch_clinical_alert=dispatch_clinical_alert,
    has_wa_link=HAS_WA_LINK,
    generate_totp_secret=generate_totp_secret if HAS_WA_LINK else None,
    log_fine_tuning_sample=log_fine_tuning_sample,
)
app.include_router(sync_router.router)

# Configuracao dos modulos core extraidos (Fase A passo 6).
# dispatch_clinical_alert continua definido neste arquivo (compartilhado
# por sync, confirmacao e WhatsApp); passamos a referencia para baixo.
confirmation_module.configure_state(
    dispatch_clinical_alert=dispatch_clinical_alert,
    send_whatsapp_interactive=send_whatsapp_interactive if HAS_WHATSAPP else None,
)
dataset_module.configure_state(
    system_prompt=SYSTEM_PROMPT,
    dataset_path=DATASET_PATH,
)

# Memoria/ORB_CHAT_PROMPT/chat_completions: ver routers/chat.py (Fase A
# passo 4). As estruturas _REST_* agora vivem la; o endpoint /v1/chat/completions
# e registrado via chat_router. assistant_memory e log_fine_tuning_sample
# permanecem aqui (compartilhados com ws_orb e WhatsApp) e sao passados ao
# router via configure_state.



async def synthesize_with_piper(text: str, voice_name: str, output_path: str) -> bool:
    """Compatibility entry point; synthesis lifecycle belongs to the Piper adapter.

    Mantido no server porque o fast-lane de audio (init_fast_lane_audio_cache)
    e os fluxos do WhatsApp (refusal + resposta de voz) o usam. O endpoint
    HTTP /v1/audio/speech + /synthesize_voice foi extraido para
    routers/audio.py (Fase A passo 3)."""
    return await PIPER_SERVICE.synthesize(text, voice_name, output_path)



# =============================================================
# ROTAS E WEBHOOK DO WHATSAPP BUSINESS CLOUD API
# =============================================================

@app.get("/api/whatsapp/status")
def whatsapp_status():
    return {
        "status": "online" if HAS_WHATSAPP else "module_missing",
        "official_number": "+55 (11) 96780-8252",
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID if HAS_WHATSAPP else None,
        "active": HAS_WHATSAPP,
        # OTP por WhatsApp esta pausado (Meta nao aprova template); o gateway
        # so despacha OTP por e-mail ate a Meta liberar a conta business.
        "otp_channel_paused": WHATSAPP_OTP_PAUSED
    }

@app.post("/api/whatsapp/send")
async def send_whatsapp_direct(req: WhatsAppSendRequest, uid: str = Depends(firebase_uid)):
    if not HAS_WHATSAPP:
        raise HTTPException(status_code=503, detail="Módulo WhatsApp não disponível no servidor")
    if req.templateName:
        res = await send_whatsapp_template(req.phone, req.templateName, req.languageCode or "en_US")
    else:
        res = await send_whatsapp_text(req.phone, req.message or "Olá! Mensagem do ecossistema Braga Saúde.")
    return res

@app.get("/api/whatsapp/webhook")
def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token")
):
    """Validação inicial exigida pelo painel da Meta para ativar o Webhook."""
    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
    if not VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN não configurado")
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print(f"[WhatsApp Webhook] Desafio de verificação validado com sucesso!")
        return PlainTextResponse(content=hub_challenge or "")
    raise HTTPException(status_code=403, detail="Token de verificação inválido")

WARM_AFFEX_KEYWORDS = [
    "obrigad", "valeu", "gratidão", "grato", "grata", "deus te abençoe", "deus abençoe",
    "fica com deus", "boa noite", "bom dia", "durma bem", "querida", "linda", "anjo",
    "amor", "carinho", "adorei", "muito bom", "maravilha", "abraço", "beijo", "beijos",
    "maravilhosa", "fofa", "coração"
]

def is_warm_and_affectionate(text: str) -> bool:
    """Detecta se a mensagem possui tom caloroso, afetuoso ou de agradecimento."""
    if not text:
        return False
    lower = text.lower()
    return any(k in lower for k in WARM_AFFEX_KEYWORDS)

# =============================================================
# ÁUDIOS ENGATILHADOS (FAST-LANE PRE-CACHED RESPONSES)
# =============================================================
# Respostas padrão de voz humanizada pré-renderizadas (cache rápido).
# Garante resposta de áudio em menos de 1.5s para saudações e dúvidas frequentes,
# liberando o modelo Qwen 2.5 72B para focar nas respostas clínicas complexas.

FAST_LANE_AUDIO_INTENTS = {
    "ask_audio": {
        "text": "Com certeza! Eu adoro conversar por áudio. Você pode me mandar áudios sempre que preferir, e eu te respondo por áudio também! Como posso te ajudar hoje?",
        "keywords": [
            "manda audio", "manda áudio", "mandar audio", "mandar áudio",
            "responde em audio", "responde em áudio", "falar por audio", "falar por áudio",
            "consegue mandar audio", "consegue me mandar audio", "consegue me mandar áudio",
            "voce manda audio", "você manda áudio", "manda por audio", "manda por áudio",
            "pode falar por audio", "pode falar por áudio", "responde audio", "responde áudio"
        ]
    },
    "greeting_morning": {
        "text": "Bom dia! Tudo bem com você? Como posso te ajudar a cuidar da sua saúde e bem-estar hoje?",
        "keywords": ["bom dia", "otimo dia", "ótimo dia", "excelente dia", "um bom dia"]
    },
    "greeting_afternoon": {
        "text": "Boa tarde! Tudo bem por aí? Me conta, como posso te ajudar com a sua saúde hoje?",
        "keywords": ["boa tarde", "otima tarde", "ótima tarde", "uma boa tarde"]
    },
    "greeting_evening": {
        "text": "Boa noite! Espero que o seu dia tenha sido tranquilo. Como posso te ajudar hoje?",
        "keywords": ["boa noite", "otima noite", "ótima noite", "uma boa noite", "durma bem"]
    },
    "greeting_general": {
        "text": "Olá! Tudo ótimo por aqui. É uma alegria falar com você! Me conta, como posso te ajudar hoje?",
        "keywords": ["olá", "ola", "oi braga", "tudo bem", "como vai", "fala braga", "e ai braga", "e aí braga", "oi tudo bem", "ola tudo bem"]
    },
    "gratitude": {
        "text": "Imagina, por nada! Estou sempre aqui para cuidar de você e da sua família com todo carinho. Se precisar de qualquer coisa, é só me chamar!",
        "keywords": ["obrigado", "obrigada", "valeu", "gratidão", "muito obrigado", "muito obrigada", "agradeço"]
    },
    "app_info": {
        "text": "O aplicativo Braga Saúde ainda está em fase de desenvolvimento e com carinho muito em breve estará disponível na Play Store! Por enquanto, você já pode acessar para conhecer os Termos de Uso e a Política de Privacidade. O app vai te ajudar a acompanhar sua hidratação, pressão arterial e rotina, com o objetivo principal de gerar um relatório em PDF bem organizado para você levar ao médico, tornando a avaliação e as decisões clínicas muito mais assertivas. Vou deixar o link logo abaixo para você poder conferir com calma!",
        "link_text": "https://braga-saude.web.app/",
        "keywords": ["onde baixo", "como baixo", "link do app", "link do aplicativo", "baixar o app", "onde encontro o app", "mandar o link", "como baixar", "link para baixar", "onde baixar", "baixar aplicativo"]
    }
}

CLINICAL_BYPASS_KEYWORDS = [
    "dor", "pressao", "pressão", "glicemia", "falta de ar", "remedio", "remédio",
    "sintoma", "tontura", "febre", "enjoo", "enjôo", "vomito", "vômito", "peito",
    "sangue", "insulina", "samu", "192", "posto", "ubs", "upa", "hospital",
    "infarto", "avc", "queda", "machuquei", "ajuda medica", "ajuda médica", "receita"
]

FAST_LANE_CACHE: Dict[str, Dict[str, Any]] = {}

def match_fast_lane_intent(text: str) -> Optional[str]:
    """Retorna o intent engatilhado para respostas ultra-rápidas se não houver termos clínicos."""
    if not text:
        return None
    lower = text.lower().strip()
    words = lower.split()
    if len(words) > 18:
        return None
    if any(ck in lower for ck in CLINICAL_BYPASS_KEYWORDS):
        return None

    # Verifica intents por ordem de prioridade
    for intent_key in ("ask_audio", "app_info", "gratitude", "greeting_morning", "greeting_afternoon", "greeting_evening", "greeting_general"):
        for kw in FAST_LANE_AUDIO_INTENTS[intent_key]["keywords"]:
            if kw in lower:
                return intent_key
    return None

async def init_fast_lane_audio_cache():
    """Pré-sintetiza os áudios engatilhados e pré-carrega em memória e na Meta."""
    print("[Fast-Lane Audio] Inicializando cache de áudios rápidos...")
    for intent_key, data in FAST_LANE_AUDIO_INTENTS.items():
        audio_path = os.path.join(AUDIO_CACHE_DIR, f"fastlane_{intent_key}.wav")
        file_bytes = None
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            try:
                with open(audio_path, "rb") as f:
                    file_bytes = f.read()
            except Exception as e:
                print(f"[Fast-Lane Audio] Erro lendo cache em disco de {intent_key}: {e}")

        if not file_bytes:
            try:
                await synthesize_with_piper(data["text"], "faber", audio_path)
                with open(audio_path, "rb") as f:
                    file_bytes = f.read()
                print(f"[Fast-Lane Audio] Áudio gerado e salvo: {audio_path} ({len(file_bytes)} bytes)")
            except Exception as e:
                print(f"[Fast-Lane Audio] Erro ao sintetizar áudio de {intent_key}: {e}")

        if file_bytes:
            FAST_LANE_CACHE[intent_key] = {
                "text": data["text"],
                "bytes": file_bytes,
                "media_id": None
            }
            if HAS_WHATSAPP:
                try:
                    m_id = await upload_whatsapp_media(file_bytes, mime_type="audio/mpeg", filename=f"fastlane_{intent_key}.mp3")
                    # NOTE: os bytes sao WAV (Piper gera RIFF/WAVE). A extensao
                    # .mp3 no nome enviado a Meta e intencional por enquanto:
                    # audio/wav nao esta na lista oficial de tipos suportados
                    # para upload. Ver doc 06 (divida rebaixada a parcial).
                    if m_id:
                        FAST_LANE_CACHE[intent_key]["media_id"] = m_id
                        print(f"[Fast-Lane Audio] Meta media_id pronto para {intent_key}: {m_id}")
                except Exception as m_err:
                    print(f"[Fast-Lane Audio] Pré-upload Meta falhou para {intent_key}: {m_err}")

# Memória curta de clarificação do WhatsApp (Fase 4): telefone -> kind.
_WA_MEMORY: Dict[str, tuple] = {}
# Última fala template do WhatsApp (anti-repetição): telefone -> fala.
_WA_LASTFALA: Dict[str, str] = {}
# Repetições de sintoma no WhatsApp (escalada anti-loop).
_WA_SYMPTOM: Dict[str, tuple] = {}

# Métrica -> ação do fluxo de confirmação (inverso do metric_type_map).
_WA_METRIC_TO_ACTION = {
    "PRESSAO": "REGISTRAR_PRESSAO",
    "GLICEMIA": "REGISTRAR_GLICEMIA",
    "AGUA": "REGISTRAR_AGUA",
    "PESO": "REGISTRAR_PESO",
    "BATIMENTOS": "REGISTRAR_BATIMENTOS",
    "OXIGENACAO": "REGISTRAR_OXIGENACAO",
}


async def _wa_send_router_text(sender_phone: str, msg_type: str, fala: str,
                               user_text: str, user_name) -> None:
    """Resposta direta do router no WhatsApp (áudio se a entrada foi áudio)."""
    try:
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=fala,
            source="whatsapp_voice_router" if msg_type in ("audio", "voice") else "whatsapp_router",
            system_prompt="ORB router Fase 4 (template, sem LLM)",
            user_name=user_name,
            action="ROUTER",
            action_params={}
        )
    except Exception:
        pass
    if msg_type in ("audio", "voice"):
        try:
            spoken = clean_text_for_tts(fala)
            wav_temp = f"/tmp/wa_router_{int(time.time()*1000)}.wav"
            if await synthesize_with_piper(spoken, "faber", wav_temp) and os.path.exists(wav_temp):
                with open(wav_temp, "rb") as f:
                    audio_bytes = f.read()
                try:
                    os.remove(wav_temp)
                except Exception:
                    pass
                if audio_bytes:
                    a_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_bytes))
                    if a_res.get("success"):
                        return
        except Exception as tts_err:
            print(f"[WhatsApp Router Audio Error] {tts_err}")
    await send_whatsapp_text(sender_phone, fala)


async def handle_wa_health_confirmation(*, sender_phone: str, user_id: str, user_name,
                                        msg_type: str, db, action_name: str,
                                        action_params: dict, reply_text: str,
                                        user_text: str, system_label: str) -> bool:
    """Fluxo de confirmação com botões (extraído do caminho LLM, Fase 4).

    Retorna True quando resolveu (valor inválido avisado ou botões
    enviados); False quando o chamador deve seguir o fluxo normal.
    """
    health_actions = ["REGISTRAR_PRESSAO", "REGISTRAR_GLICEMIA", "REGISTRAR_AGUA",
                      "REGISTRAR_PESO", "REGISTRAR_BATIMENTOS", "REGISTRAR_OXIGENACAO"]
    if action_name not in health_actions or not user_id:
        return False
    metric_type_map = {
        "REGISTRAR_PRESSAO": "PRESSAO",
        "REGISTRAR_GLICEMIA": "GLICEMIA",
        "REGISTRAR_AGUA": "AGUA",
        "REGISTRAR_PESO": "PESO",
        "REGISTRAR_BATIMENTOS": "BATIMENTOS",
        "REGISTRAR_OXIGENACAO": "OXIGENACAO"
    }
    metric_type = metric_type_map.get(action_name)
    if not metric_type:
        return False
    is_valid, error_msg, validated_params = validate_health_data(metric_type, action_params)
    if not is_valid:
        await send_whatsapp_text(sender_phone,
            f"⚠️ *Valor inválido:*\n{error_msg}\n\n"
            f"Por favor, envie novamente o valor correto."
        )
        return True
    confirmation_code = await create_pending_confirmation(
        user_id=user_id,
        metric_type=metric_type,
        value_json=validated_params,
        db_pool=db
    )
    if confirmation_code:
        await send_confirmation_buttons(
            to_number=sender_phone,
            metric_type=metric_type,
            values=validated_params,
            confirmation_code=confirmation_code,
            user_name=user_name
        )
        print(f"[WhatsApp Confirmation] Botões enviados: {confirmation_code} para {metric_type}")
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=reply_text,
            source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
            system_prompt=system_label,
            user_name=user_name,
            action=action_name,
            action_params={**action_params, "pending_confirmation": True, "confirmation_code": confirmation_code}
        )
        return True
    print(f"[WhatsApp Confirmation] Erro ao criar pendência, enviando resposta normal")
    return False

async def process_incoming_whatsapp_payload(payload: Dict[str, Any]):
    """Processa mensagens recebidas no WhatsApp e responde usando a IA Qwen 2.5 72B."""
    if not HAS_WHATSAPP:
        return
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                val = change.get("value", {})
                messages = val.get("messages", [])
                for msg in messages:
                    msg_id = msg.get("id")
                    msg_type = msg.get("type")
                    sender_phone = msg.get("from")
                    user_text = ""

                    # Marca a mensagem como lida (dois risquinhos azuis no WhatsApp)
                    if msg_id:
                        asyncio.create_task(mark_whatsapp_message_read(msg_id))

                    if msg_type == "text":
                        user_text = msg.get("text", {}).get("body", "").strip()
                    elif msg_type in ("audio", "voice"):
                        audio_info = msg.get("audio") or msg.get("voice") or {}
                        media_id = audio_info.get("id")
                        if media_id:
                            print(f"[WhatsApp Audio] Processando áudio ID {media_id} de {sender_phone}...")
                            audio_bytes = await download_whatsapp_media(media_id)
                            if audio_bytes:
                                user_text = await transcribe_audio_whisper(audio_bytes, GROQ_API_KEY_WHATSAPP) or ""
                                print(f"[WhatsApp Audio] Transcrição concluída: '{user_text}'")
                        if not user_text:
                            await send_whatsapp_text(
                                sender_phone,
                                "Olá! Recebi o seu áudio, mas não consegui escutar nitidamente. Pode me mandar de novo ou escrever por texto? 💚"
                            )
                            continue

                    if not sender_phone or not user_text:
                        continue

                    wa_tid = trace_llm('whatsapp', 'request', user_text=user_text,
                                       msg_type=msg_type, phone_tail=(sender_phone or '')[-4:])

                    print(f"[WhatsApp Webhook] Mensagem recebida de {sender_phone} ({msg_type}): {user_text}")

                    # Reação humanizada e sutil: se a mensagem tiver tom carinhoso/amoroso/agradecimento, reage com coração verde 💚
                    if msg_id and is_warm_and_affectionate(user_text):
                        asyncio.create_task(send_whatsapp_reaction(sender_phone, msg_id, "💚"))

                    # 1. Blindagem contra Prompt Injection ou Tarefas Exaustivas (ex: contar até 100)
                    if is_prompt_injection(user_text):
                        refusal_text = (
                            "Ih, contar até aí vai demorar uma eternidade! 😅 "
                            "Meu foco por aqui é cuidar com todo carinho da sua saúde e bem-estar. "
                            "Me conta, como você está se sentindo hoje? 💚"
                        )
                        if msg_type in ("audio", "voice"):
                            audio_sent = False
                            try:
                                # Unificado no Piper (decisao 20/09/2026, doc 11 §3.1):
                                # edge_tts nunca foi importado neste arquivo -> o NameError
                                # era engolido aqui e a recusa caia sempre em texto.
                                spoken_refusal = clean_text_for_tts(refusal_text)
                                refusal_path = os.path.join(AUDIO_CACHE_DIR, f"refusal_{hashlib.md5(spoken_refusal.encode('utf-8')).hexdigest()}.wav")
                                if await synthesize_with_piper(spoken_refusal, "faber", refusal_path) and os.path.exists(refusal_path):
                                    with open(refusal_path, "rb") as f:
                                        audio_buf = bytearray(f.read())
                                    if audio_buf:
                                        a_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_buf))
                                        if a_res.get("success"):
                                            audio_sent = True
                            except Exception as e:
                                print(f"[Refusal Audio Error] {e}")
                            if not audio_sent:
                                await send_whatsapp_text(sender_phone, refusal_text)
                        else:
                            await send_whatsapp_text(sender_phone, refusal_text)
                        continue

                    # 2. Identificação do Usuário no PostgreSQL
                    # Primária: whatsapp_phone (vínculo verificado por TOTP, new).
                    # Fallback (transição): busca legada por sufixo de 8 dígitos,
                    # mantida até as contas existentes migrarem para o vínculo novo.
                    #
                    # AUD-G10: o fallback compara os ÚLTIMOS 8 dígitos contra
                    # phone/emergency_contact_phone com LIKE '%sufixo%' — o que
                    # casa QUALQUER aparição do sufixo em QUALQUER posição. Dois
                    # números diferentes que coincidem nos 8 finais (ou até
                    # contém o trecho) apontavam para o mesmo paciente, e um
                    # estranho podia se passar por usuário cadastrado. Agora a
                    # associação legada exige o número COMPLETO (mesma
                    # quantidade de dígitos), e logswarn avisam que o caminho
                    # ainda está em uso para que a migração seja concluída.
                    user_id = None
                    user_name = None
                    user_role = None
                    if db_pool:
                        try:
                            _wa_digits = "".join(filter(str.isdigit, sender_phone or ""))
                            async with db_pool.acquire() as conn:
                                p_row = await conn.fetchrow(
                                    "SELECT id, full_name, user_role FROM profiles "
                                    "WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1",
                                    _wa_digits
                                )
                                if not p_row and _wa_digits:
                                    # AUD-G10: match EXATO do número completo
                                    # (igualdade de dígitos), nunca substring.
                                    p_row = await conn.fetchrow(
                                        "SELECT id, full_name, user_role FROM profiles "
                                        "WHERE regexp_replace(phone, '\\D', '', 'g') = $1",
                                        _wa_digits
                                    )
                                    if not p_row:
                                        p_row = await conn.fetchrow(
                                            "SELECT id, full_name, user_role FROM profiles "
                                            "WHERE regexp_replace(emergency_contact_phone, '\\D', '', 'g') = $1",
                                            _wa_digits
                                        )
                                    if p_row:
                                        print(
                                            f"[AUD-G10] Conta {p_row['id']} acessada pelo fallback "
                                            f"legado (phone completo) — número sem vínculo TOTP. "
                                            f"Migrar para whatsapp_link."
                                        )
                                if p_row:
                                    user_id = p_row["id"]
                                    user_name = p_row["full_name"]
                                    user_role = p_row["user_role"]
                        except Exception as db_err:
                            print(f"[WhatsApp DB Lookup Error] {db_err}")

                    # 2.5. VÍNCULO TOTP (arquitetura temporária, sem template).
                    # Roda ANTES do portão de conta: é assim que um número sem conta
                    # conquista a dele. Se consumir, já respondeu e paramos por aqui.
                    if HAS_WA_LINK:
                        try:
                            _consumed = await handle_link_attempt(
                                sender_phone=sender_phone,
                                text=user_text,
                                db_pool=db_pool,
                                send_text=send_whatsapp_text,
                            )
                            if _consumed:
                                continue
                        except Exception as _link_err:
                            print(f"[WhatsApp Link Error] {_link_err}")

                    # 2.6. PORTÃO DE CONTA VINCULADA: sem conta -> institucional,
                    # MAS a triagem de emergência (SAMU) permanece ativa antes dele.
                    # Ordem: vinculo -> emergência -> portão -> fluxo normal.
                    if HAS_WA_GATE:
                        try:
                            _blocked = await gate_unregistered(
                                user_text=user_text,
                                user_id=user_id,
                                send_text=send_whatsapp_text,
                                sender_phone=sender_phone,
                            )
                            if _blocked:
                                continue
                        except Exception as _gate_err:
                            print(f"[WhatsApp Gate Error] {_gate_err}")

                    # 2.7. Fast-Lane de Resposta Rápida com Áudios Engatilhados
                    # (somente após portão de conta: anônimo já respondeu antes)
                    fast_intent = match_fast_lane_intent(user_text)
                    if fast_intent:
                        cached = FAST_LANE_CACHE.get(fast_intent)
                        if not cached or not cached.get("bytes"):
                            data = FAST_LANE_AUDIO_INTENTS[fast_intent]
                            text_hash = hashlib.md5(data["text"].encode("utf-8")).hexdigest()[:8]
                            audio_path = os.path.join(AUDIO_CACHE_DIR, f"fastlane_{fast_intent}_{text_hash}.mp3")
                            f_bytes = None
                            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                                with open(audio_path, "rb") as f:
                                    f_bytes = f.read()
                            if not f_bytes:
                                try:
                                    # Unificado no Piper (decisao 20/09/2026, doc 11 §3.1):
                                    # antes usava edge_tts aqui, mas edge_tts nunca foi
                                    # importado neste arquivo -> NameError silencioso.
                                    # O Piper ja e a engine do fast-lane de startup
                                    # (init_fast_lane_audio_cache) e da voz em producao.
                                    ok = await synthesize_with_piper(data["text"], "faber", audio_path)
                                    if ok:
                                        with open(audio_path, "rb") as f:
                                            f_bytes = f.read()
                                except Exception as e:
                                    print(f"[Fast-Lane On-Demand Error] {e}")
                            cached = {"text": data["text"], "bytes": f_bytes, "media_id": None}
                            FAST_LANE_CACHE[fast_intent] = cached

                        reply_text = cached["text"]
                        audio_bytes = cached.get("bytes")
                        media_id = cached.get("media_id")

                        print(f"[WhatsApp Fast-Lane] Intent '{fast_intent}' engatilhado para {sender_phone}!")

                        # Registra no dataset de fine-tuning (higienizado por LGPD)
                        log_fine_tuning_sample(
                            user_prompt=user_text,
                            assistant_reply=reply_text,
                            source="whatsapp_voice_fastlane" if msg_type in ("audio", "voice") else "whatsapp_fastlane",
                            system_prompt="Resposta rápida padronizada Braga Saúde (Voz Antonio)",
                            user_name=user_name,
                            action="FAST_LANE",
                            action_params={"intent": fast_intent}
                        )

                        if msg_type in ("audio", "voice"):
                            audio_res = await send_whatsapp_audio(
                                sender_phone,
                                audio_bytes=audio_bytes,
                                media_id=media_id
                            )
                            if audio_res.get("success"):
                                if audio_res.get("media_id"):
                                    cached["media_id"] = audio_res["media_id"]
                                # Send link if it exists
                                if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                    await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                                continue
                            # Fallback para texto se áudio falhar
                            await send_whatsapp_text(sender_phone, reply_text)
                            if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                            continue
                        else:
                            await send_whatsapp_text(sender_phone, reply_text)
                            if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                            continue

                    # 2.7. Camada 0 (Fase 4): router determinístico antes do LLM.
                    # Vitais com número vão direto aos botões; fofoca/social vai
                    # de template; só conversa aberta chega ao Groq.
                    wa_mem_key = "".join(filter(str.isdigit, sender_phone or "")) or sender_phone
                    wa_follow = resolve_followup(user_text, recall(_WA_MEMORY, wa_mem_key), channel="wa")
                    if wa_follow is not None:
                        _WA_MEMORY.pop(wa_mem_key, None)
                        wa_follow["fala"] = dedupe_fala(_WA_LASTFALA, wa_mem_key, wa_follow["fala"])
                        trace_llm('whatsapp', 'followup_hit', wa_tid, fala=wa_follow["fala"])
                        await _wa_send_router_text(sender_phone, msg_type, wa_follow["fala"], user_text, user_name)
                        continue
                    wa_kind = detect_all(user_text).get("kind")
                    wa_route = whatsapp_next(user_text, bool(user_id))
                    trace_llm('whatsapp', 'router_hit', wa_tid, route={k: (str(v)[:200]) for k, v in wa_route.items()})
                    if wa_route["type"] == "text":
                        if wa_kind == "nutrition":
                            remember(_WA_MEMORY, wa_mem_key, "nutrition")
                        if wa_kind in SYMPTOM_KINDS:
                            if symptom_repeat(_WA_SYMPTOM, wa_mem_key, wa_kind) >= 2:
                                wa_route["fala"] += ESCALATION_SUFFIX
                        wa_route["fala"] = dedupe_fala(_WA_LASTFALA, wa_mem_key, wa_route["fala"])
                        await _wa_send_router_text(sender_phone, msg_type, wa_route["fala"], user_text, user_name)
                        continue
                    if wa_route["type"] == "vitals" and user_id:
                        _wa_action = _WA_METRIC_TO_ACTION.get(wa_route["metric"])
                        _wa_handled = await handle_wa_health_confirmation(
                            sender_phone=sender_phone, user_id=user_id, user_name=user_name,
                            msg_type=msg_type, db=db_pool, action_name=_wa_action,
                            action_params=wa_route["values"], reply_text=wa_route["fala"],
                            user_text=user_text, system_label="ORB router Fase 4 (vitals, sem LLM)"
                        )
                        if _wa_handled:
                            continue
                        await _wa_send_router_text(
                            sender_phone, msg_type,
                            "Tive um problema técnico para preparar sua confirmação. Tente de novo em instantes.",
                            user_text, user_name)
                        continue

                    # Política compartilhada; dados do usuário não entram no system prompt.
                    conv_prompt = build_conversation_system("whatsapp")

                    # 4. Resposta com IA Qwen (Groq)
                    headers = {
                        "Authorization": f"Bearer {GROQ_API_KEY_WHATSAPP}",
                        "Content-Type": "application/json"
                    }
                    messages_payload = [
                        {"role": "system", "content": conv_prompt},
                        {"role": "user", "content": user_text}
                    ]
                    data = {
                        "model": MODEL_WHATSAPP,
                        "messages": messages_payload,
                        "max_tokens": 500,
                        "temperature": 0.6
                    }
                    trace_llm('whatsapp', 'llm_request', wa_tid, model=MODEL_ID,
                              max_tokens=500, temperature=0.6, messages=messages_payload)

                    async with SEM_WA:
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            resp = await client.post(GROQ_API_URL, headers=headers, json=data)
                        if resp.status_code == 200:
                            r_json = resp.json()
                            reply_text = r_json["choices"][0]["message"]["content"]
                            action_name = "CONVERSA"
                            action_params = {}
                            try:
                                raw_c = reply_text.strip()
                                if "```" in raw_c:
                                    m_code = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_c)
                                    if m_code:
                                        raw_c = m_code.group(1).strip()
                                s_idx = raw_c.find('{')
                                e_idx = raw_c.rfind('}')
                                if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                                    raw_c = raw_c[s_idx:e_idx+1]
                                parsed = json.loads(raw_c)
                                reply_text = parsed.get("fala") or parsed.get("message") or reply_text
                                action_name = parsed.get("acao", "CONVERSA")
                                action_params = parsed.get("parametros", {})
                            except Exception:
                                pass
                            trace_llm('whatsapp', 'llm_response', wa_tid, reply_text=reply_text,
                                      action=action_name, params=action_params)
                            
                            # FLUXO DE CONFIRMAÇÃO (função compartilhada com o router, Fase 4)
                            _handled = await handle_wa_health_confirmation(
                                sender_phone=sender_phone, user_id=user_id, user_name=user_name,
                                msg_type=msg_type, db=db_pool, action_name=action_name,
                                action_params=action_params, reply_text=reply_text,
                                user_text=user_text, system_label=conv_prompt
                            )
                            if _handled:
                                return  # Não envia resposta de texto - aguarda confirmação
                            
                            # Registra no dataset de fine-tuning (higienizado por LGPD)
                            log_fine_tuning_sample(
                                user_prompt=user_text,
                                assistant_reply=reply_text,
                                source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
                                system_prompt=conv_prompt,
                                user_name=user_name,
                                action=action_name,
                                action_params=action_params
                            )

                            # Extrai links da resposta usando Regex
                            links_to_send = re.findall(r'\[LINK:\s*(.*?)\]', reply_text)
                            # Higieniza profundamente o texto falado para a IA não soletrar expressões/caracteres
                            spoken_text = clean_text_for_tts(reply_text)

                            # REGRA CLÍNICA & ACESSIBILIDADE: Toda mensagem de áudio é respondida em ÁUDIO!
                            if msg_type in ("audio", "voice"):
                                audio_sent = False
                                try:
                                    print(f"[WhatsApp TTS] Sintetizando resposta de voz do Braga (Piper faber)...")
                                    wav_temp = f"/tmp/wa_reply_{int(time.time()*1000)}.wav"
                                    ok = await synthesize_with_piper(spoken_text, "faber", wav_temp)
                                    audio_buffer = bytearray()
                                    if ok and os.path.exists(wav_temp):
                                        with open(wav_temp, "rb") as f:
                                            audio_buffer = bytearray(f.read())
                                        try: os.remove(wav_temp)
                                        except: pass

                                    if audio_buffer:
                                        print(f"[WhatsApp Audio Upload] Enviando áudio de {len(audio_buffer)} bytes...")
                                        audio_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_buffer))
                                        if audio_res.get("success"):
                                            audio_sent = True
                                            print(f"[WhatsApp Audio] Áudio do Braga entregue com sucesso!")
                                            for link in links_to_send:
                                                await send_whatsapp_text(sender_phone, link)
                                except Exception as tts_err:
                                    print(f"[WhatsApp Audio Synthesize/Send Error] {tts_err}")

                                # Se o áudio falhar por qualquer motivo, garante resposta por texto
                                if not audio_sent:
                                    if links_to_send:
                                        await send_whatsapp_text(sender_phone, f"{spoken_text}\n\n{links_to_send[0]}")
                                    else:
                                        await send_whatsapp_text(sender_phone, spoken_text)
                            else:
                                if links_to_send:
                                    await send_whatsapp_text(sender_phone, f"{spoken_text}\n\n{links_to_send[0]}")
                                else:
                                    await send_whatsapp_text(sender_phone, spoken_text)
                        else:
                            await send_whatsapp_text(
                                sender_phone,
                                "Olá! Recebi sua mensagem com carinho, mas meu sistema está processando muitas informações agora. Já já volto a te responder!"
                            )
    except Exception as e:
        print(f"[WhatsApp Webhook Error] Falha ao processar mensagem recebida: {e}")

@app.post("/api/whatsapp/webhook")
async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Endpoint receptor de eventos e mensagens da Meta Cloud API."""
    # Zero-trust: sem a assinatura HMAC do App Secret, qualquer um forja
    # webhooks e se passa por qualquer remetente (confirmacoes de registros
    # de saude inclusas). payload bruto e necessario para o HMAC.
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256") or request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(signature, raw_body):
        print("[WhatsApp Webhook] Assinatura invalida ou ausente — requisicao rejeitada.")
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    try:
        body = json.loads(raw_body.decode("utf-8"))
        # AUD-G(M): persiste o evento BRUTO no outbox ANTES de ackar. Se o
        # processo cair entre este ponto e o retorno 200, a mensagem se perde
        # (a Meta não reenvia quem ackou). Falha aqui é só warning — melhor
        # processar sem registro que perder a mensagem inteira.
        try:
            with open(WEBHOOK_OUTBOX_PATH, "a", encoding="utf-8") as _f:
                _f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat(),
                    "payload": body,
                }, ensure_ascii=False) + "\n")
        except Exception as _e:
            print(f"[WhatsApp Webhook] Aviso: não gravou no outbox: {_e}")

        WHATSAPP_EVENT_LOG.append({
            "timestamp": datetime.utcnow().isoformat(),
            "payload": body
        })
        if len(WHATSAPP_EVENT_LOG) > 200:
            WHATSAPP_EVENT_LOG.pop(0)
        
        # Detectar cliques em botões interativos (confirmação)
        if "entry" in body:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for msg in messages:
                        if msg.get("type") == "interactive":
                            interactive = msg.get("interactive", {})
                            if interactive.get("type") == "button":
                                button_id = interactive.get("button", {}).get("id", "")
                                sender_phone = msg.get("from", "")
                                if button_id and sender_phone:
                                    background_tasks.add_task(
                                        handle_confirmation_callback,
                                        button_id=button_id,
                                        sender_phone=sender_phone
                                    )
                                    return JSONResponse({"status": "callback_received"}, status_code=200)
    except Exception:
        return JSONResponse({"status": "invalid_payload"}, status_code=400)

    background_tasks.add_task(process_incoming_whatsapp_payload, body)
    return JSONResponse({"status": "received"}, status_code=200)


async def handle_confirmation_callback(button_id: str, sender_phone: str):
    """Processa cliques nos botões de confirmação."""
    try:
        # Extrair ação e código do button_id (formato: action_code)
        parts = button_id.split("_", 1)
        if len(parts) != 2:
            return
        
        action, confirmation_code = parts
        action = action.lower()
        
        # Buscar pool de conexão
        if not db_pool:
            print("[Callback Error] db_pool não disponível")
            return

        # AUD-G(M): a função expire_pending_confirmations() existia no banco
        # (migration 004:63-71) mas NINGUÉM a chamava — confirmações vencidas
        # ficavam com status='pending' para sempre, poluindo o índice parcial
        # idx_pending_conf_expires e o relatório v_confirmation_report.
        # Expira aqui (cada callback é o momento natural de limpeza).
        try:
            async with db_pool.acquire() as _c:
                await _c.execute("SELECT expire_pending_confirmations()")
        except Exception as _e:
            print(f"[Callback] Aviso: não expirou pendências vencidas: {_e}")
        
        # Buscar usuário pelo telefone
        # AUD-G10: era LIKE '%' || $1 com os últimos 8 dígitos — correspondência
        # parcial que podia caslar a conta errada. Agora exige o número completo.
        user_id = None
        _cb_digits = "".join(filter(str.isdigit, sender_phone or ""))
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id FROM profiles
                WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1
                   OR regexp_replace(phone, '\\D', '', 'g') = $1
                LIMIT 1
            """, _cb_digits)
            if not row:
                row = await conn.fetchrow("""
                    SELECT id FROM profiles
                    WHERE regexp_replace(emergency_contact_phone, '\\D', '', 'g') = $1
                    LIMIT 1
                """, _cb_digits)
            if row:
                user_id = row['id']
        
        if not user_id:
            print(f"[Callback] Usuário não encontrado para telefone: {sender_phone}")
            await send_whatsapp_text(sender_phone, "Usuário não encontrado. Verifique seu cadastro.")
            return
        
        # Processar ação
        if action == "confirm":
            success = await confirm_pending_confirmation(confirmation_code, user_id, db_pool)
            if success:
                # Buscar dados da confirmação para a mensagem final
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        SELECT metric_type, value_json, confirmed_at
                        FROM pending_confirmations 
                        WHERE confirmation_code = $1
                    """, confirmation_code)
                    
                    if row:
                        metric_type = row['metric_type']
                        values = json.loads(row['value_json'])
                        
                        # Formatar mensagem de sucesso
                        if metric_type == "PRESSAO":
                            value_display = f"{values.get('sistolica')} x {values.get('diastolica')} mmHg"
                        elif metric_type == "GLICEMIA":
                            value_display = f"{values.get('glicemia')} mg/dL"
                        elif metric_type == "AGUA":
                            value_display = f"{values.get('quantidade_ml')} ml"
                        elif metric_type == "PESO":
                            value_display = f"{values.get('peso_kg')} kg"
                        else:
                            value_display = str(values)
                        
                        await send_whatsapp_text(sender_phone, 
                            f"✅ *Registro confirmado por você!*\n\n"
                            f"📋 Código: `{confirmation_code}`\n"
                            f"🩺 {metric_type}: {value_display}\n"
                            f"🕐 Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                            f"Este registro foi autenticado por você e pode ser consultado no aplicativo."
                        )
            else:
                await send_whatsapp_text(sender_phone, 
                    "❌ Não foi possível confirmar. O registro pode ter expirado.\n"
                    "Por favor, envie os dados novamente."
                )
        
        elif action == "edit":
            # Buscar tipo de métrica para orientar o usuário
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT metric_type FROM pending_confirmations 
                    WHERE confirmation_code = $1 AND status = 'pending'
                """, confirmation_code)
                
                if row:
                    metric_type = row['metric_type']
                    await cancel_pending_confirmation(confirmation_code, user_id, db_pool)
                    
                    if metric_type == "PRESSAO":
                        await send_whatsapp_text(sender_phone, 
                            "✏️ *Edição de Pressão*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *130 por 85*"
                        )
                    elif metric_type == "GLICEMIA":
                        await send_whatsapp_text(sender_phone, 
                            "✏️ *Edição de Glicose*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *glicose 120*"
                        )
                    elif metric_type == "AGUA":
                        await send_whatsapp_text(sender_phone, 
                            "✏️ *Edição de Água*\n\n"
                            "Por favor, envie novamente a quantidade correta.\n"
                            "Exemplo: *bebi 300ml de água*"
                        )
                    elif metric_type == "PESO":
                        await send_whatsapp_text(sender_phone, 
                            "✏️ *Edição de Peso*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *meu peso 75kg*"
                        )
        
        elif action == "cancel":
            success = await cancel_pending_confirmation(confirmation_code, user_id, db_pool)
            if success:
                await send_whatsapp_text(sender_phone, 
                    "❌ *Registro cancelado.*\n\n"
                    "Seus dados não foram salvos.\n"
                    "Se precisar, pode enviar novamente a qualquer momento."
                )
            else:
                await send_whatsapp_text(sender_phone, 
                    "Não foi possível cancelar. O registro pode já ter sido processado."
                )
        
        else:
            print(f"[Callback] Ação desconhecida: {action}")
    
    except Exception as e:
        print(f"[Callback Error] Falha ao processar callback: {e}")
        await send_whatsapp_text(sender_phone, 
            "Desculpe, tive um problema ao processar sua solicitação.\n"
            "Por favor, tente novamente."
        )

@app.get("/api/whatsapp/events")
def whatsapp_events(limit: int = Query(10, ge=1, le=50), uid: str = Depends(firebase_uid)):
    """Retorna os últimos eventos brutos recebidos pelo webhook do WhatsApp para diagnóstico.

    Payloads conteem telefones e corpos de mensagens (PII) DE TODOS OS USUARIOS:
    qualquer conta autenticada podia ler as mensagens dos outros. AUD-G09:
    agora exige flag de administrador no perfil.
    """
    if not db_pool:
        raise HTTPException(503, "Banco de dados não disponível")
    try:
        role = None
        with db_pool.acquire() as _conn:
            role = _conn.fetchval("SELECT user_role FROM profiles WHERE id = $1", uid)
    except Exception:
        pass
    if role != "ADMIN":
        raise HTTPException(403, "Acesso restrito a administradores.")
    return {
        "total_eventos_recebidos": len(WHATSAPP_EVENT_LOG),
        "eventos": list(reversed(WHATSAPP_EVENT_LOG[-limit:]))
    }

# =============================================================
# ENDPOINTS DE SEGURANÇA E AUTENTICAÇÃO OMNICHANNEL (OTP)
# =============================================================

# ENDPOINTS DE AUTENTICACAO (/api/auth/*): ver routers/auth.py
# (Fase A passo 4b). OTP send/verify, password/reset, welcome-email,
# check-account. Nao redeclarar aqui.




# =============================================================

# ENDPOINTS DE DISTRIBUICAO E ATUALIZACAO DO APP (OTA) + LANDING
# ver routers/app.py (Fase A passo 3): /api/app/latest, /api/app/download,
# /api/app/version, /api/app/notify-update, /
# RELEASES_DIR vem de `config`.

if os.path.isdir(app_router.ONBOARDING_DIR):
    app.mount("/", StaticFiles(directory=app_router.ONBOARDING_DIR, html=True), name="onboarding")
