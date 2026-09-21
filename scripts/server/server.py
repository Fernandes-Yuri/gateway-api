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
# Routers extraidos (Fase A passo 7): WhatsApp Business Cloud API.
# O pipeline completo (webhook, router Fase 4, fast-lane de audio,
# callback de confirmacao) vive la; este arquivo so registra.
from routers import whatsapp as whatsapp_router
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
# WHATSAPP_OTP_PAUSED mora em routers/whatsapp.py (Fase A passo 7); o
# auth_router le via whatsapp_router.WHATSAPP_OTP_PAUSED.

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
        asyncio.create_task(whatsapp_router.init_fast_lane_audio_cache())

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
    asyncio.create_task(whatsapp_router.init_fast_lane_audio_cache())

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
    whatsapp_otp_paused=whatsapp_router.WHATSAPP_OTP_PAUSED,
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

# Registro do router do WhatsApp (Fase A passo 7). O fast-lane de audio
# continua sendo inicializado no startup daqui (init_fast_lane_audio_cache).
whatsapp_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    piper_service=PIPER_SERVICE,
    sem_wa=SEM_WA,
)
app.include_router(whatsapp_router.router)

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



# =============================================================
# WHATSAPP BUSINESS CLOUD API: ver routers/whatsapp.py (Fase A passo 7).
# Webhook (GET desafio + POST com HMAC), pipeline de mensagem entrante
# (audio -> router Fase 4 -> Groq), fast-lane de audio, callback de
# confirmacao e /api/whatsapp/{status,send,events}. O fast-lane e
# inicializado no startup via whatsapp_router.init_fast_lane_audio_cache().
# Nao redeclarar aqui.
# =============================================================

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
