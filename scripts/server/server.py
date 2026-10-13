import os
import asyncio
from typing import List, Optional, Any
from fastapi import BackgroundTasks, FastAPI, HTTPException as HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from assistant_preferences import install_preference_routes, process_preferences
from family_api import install_family_routes, purge_expired_family_messages
from caregiver_alerts import install_caregiver_alert_routes
from security_defense import (
    StrictRateLimiter,
    get_client_ip,
    is_prompt_injection,
)

# Nucleo extraido (Fase A passo 1): sanitizacao de TTS e PII (LGPD).
# Nucleo extraido (Fase A passo 2, Fase B): helpers de dispositivos, tokens FCM e push.
from core.devices import (
    dispatch_push,
    get_active_tokens_for_caregivers,
    load_devices,
    migrate_legacy_json_devices,
)
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
# Router administrativo do portal webservice (D-WS1 / D50): leituras e
# operacoes admin protegidas por token de servico. O portal Next.js no
# Render consome estes endpoints e deixa de acessar o RDS diretamente.
from routers import admin as admin_router
from routers import care_os as care_os_router
from routers import catalog as catalog_router
from services.care_automations import (
    send_nightly_bulletins,
    send_pattern_break_alerts,
    send_stock_alerts,
)
# Modulos core extraidos (Fase A passo 6): confirmacao de leituras vitais
# (maquina de pendencias + validacao clinica + botes do WhatsApp) e
# curadoria do dataset de fine-tuning.
import core.confirmation as confirmation_module
import core.dataset as dataset_module
from core.dataset import (
    log_fine_tuning_sample,
)


# Configuração central (Fase B, passo 1 — doc 36): env vars, defaults e paths.
from config import (
    DATASET_PATH, RELEASES_DIR,
    DATABASE_URL,
    GROQ_API_URL, MODEL_ID, MODEL_REST, GROQ_API_KEY_CHAT,
    SERVICE_ACCOUNT_PATH,
    PUBLIC_API_BASE_URL,
    DEVELOPMENT_MODE,
)

# Suporte defensivo a E-mail de Segurança & OTP
try:
    from email_service import is_email_configured, send_security_code_email, send_welcome_email, send_security_alert_email
    HAS_EMAIL = True
except ImportError:
    HAS_EMAIL = False
    send_welcome_email = None

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
        build_wa_link,
        send_clinical_alert_whatsapp,
        send_security_code_whatsapp,
        send_whatsapp_interactive,
        send_whatsapp_template,
        send_whatsapp_text,
    )
    HAS_WHATSAPP = True
except ImportError:
    HAS_WHATSAPP = False
    build_wa_link = None
    send_clinical_alert_whatsapp = None
    send_security_code_whatsapp = None
    send_whatsapp_interactive = None
    send_whatsapp_template = None
    send_whatsapp_text = None

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

GROQ_SEMAPHORE = asyncio.Semaphore(5)
# Concorrência por canal (isolamento: um canal lento não mata os outros).
SEM_CHAT = asyncio.Semaphore(3)
SEM_REST = asyncio.Semaphore(2)
SEM_WA = asyncio.Semaphore(3)

# AUD-SEC/P2 (contrarrrelatório doc 07 §4 V5, 21/09/2026): /docs, /redoc e
# /openapi.json expostos em produção entregavam um mapa gratuito de 74 rotas
# (incluindo /api/admin/* e o design interno do broadcast) a qualquer atacante
# — recon zero custo. Em produção ficam desligados; localmente, DEVELOPMENT_MODE=1
# os reabilita para depuração. Não afeta os testes (não dependem do OpenAPI público).
app = FastAPI(
    title="Braga Saúde Gateway, TTS, Sync & Notification Hub",
    version="5.0",
    docs_url="/docs" if DEVELOPMENT_MODE else None,
    redoc_url="/redoc" if DEVELOPMENT_MODE else None,
    openapi_url="/openapi.json" if DEVELOPMENT_MODE else None,
)

# Routers extraidos (Fase A passo 2): o estado global fica aqui no server e e
# passado para o router via configure_state; os endpoints leem sem acoplar.
health_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    has_postgres=HAS_POSTGRES,
    has_scheduler=HAS_SCHEDULER,
    has_fcm=HAS_FCM,
    has_whatsapp=HAS_WHATSAPP,
    whatsapp_otp_paused=getattr(whatsapp_router, "WHATSAPP_OTP_PAUSED", False),
    messaging=messaging if HAS_FCM else None,
)
app.include_router(health_router.router)
app_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    messaging=messaging if HAS_FCM else None,
    has_fcm=HAS_FCM,
)
app.include_router(app_router.router)
audio_router.configure_state()
app.include_router(audio_router.router)

# Origens permitidas via env (separadas por vírgula). "CORS *" com credenciais
# é inseguro e inválido perante a spec; a lista precisa ser explícita.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://bragasaude.online,https://www.bragasaude.online",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Distribuicao publica do app: metadados de versao/hash e o proprio APK sao
# conteudo estatico sem credenciais. O site oficial e a pagina de download
# (braga-saude.web.app/app-download.html) buscam version + SHA-256 por fetch;
# sem ACAO aberto nesses caminhos o navegador engola a resposta e a pagina
# mostra "Versao indisponivel". Nao entra na allowlist da API de proposito:
# aqui nao ha cookie nem Bearer, entao "*" e seguro e nao abre o resto da API.
PUBLIC_APP_PATHS = ('/api/app/latest', '/api/app/download', '/api/app/version')


@app.middleware("http")
async def public_app_release_cors(request: Request, call_next):
    """CORS aberto só nos endpoints públicos de distribuição do APK."""
    path = request.url.path.rstrip('/') or '/'
    if path not in PUBLIC_APP_PATHS:
        return await call_next(request)
    if request.method == 'OPTIONS':
        return Response(status_code=204, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': request.headers.get(
                'access-control-request-headers', 'content-type, cache-control, pragma'),
            'Access-Control-Max-Age': '86400',
        })
    response = await call_next(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    # "*" e credenciais são incompatíveis; esses endpoints nunca usam cookie.
    if 'access-control-allow-credentials' in response.headers:
        del response.headers['access-control-allow-credentials']
    return response

db_pool: Optional[Any] = None
from user_moderation import configure_pool as configure_moderation_pool
configure_moderation_pool(lambda: db_pool)

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


@app.middleware('http')
async def user_moderation_middleware(request: Request, call_next):
    """Aplica restrições também às rotas públicas quando recebem identidade Firebase."""
    path = request.url.path
    header = request.headers.get('authorization', '')
    if (path.startswith('/api/') and not path.startswith('/api/admin/') and
            not (path == '/api/app/latest' and request.method == 'GET') and
            header.startswith('Bearer ') and header[7:].count('.') == 2):
        from api_identity import firebase_uid
        try:
            await firebase_uid(request)
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={'detail': error.detail})
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
    caregiverRole: str = "CAREGIVER_VIEWER"
    permissions: List[str] = Field(default_factory=list)
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

# Routers extraidos que precisam do pool Postgres. Eles o capturam via
# configure_state no momento do REGISTRO (import time), quando o pool ainda
# nao existe — ele so e criado no startup() abaixo. Sem o repasse explicito
# de _propagate_db_pool(), os routers ficariam com db_pool=None para sempre,
# MESMO com DATABASE_URL configurado, e toda rota de banco devolveria
# 503/offline_mode em producao. (Detectado na revisao pre-push: os testes
# locais passavam justamente por esperarem pool None offline.)
_POOL_ROUTERS = (health_router, app_router, chat_router, auth_router,
                 sync_router, whatsapp_router, admin_router, care_os_router, catalog_router)


def _propagate_db_pool():
    """Repassa o pool vivo (ou None) para os routers extraidos.

    Idempotente: chamar no startup (apos create_pool) e no shutdown
    (apos close) mantem todos os routers sincronizados com o ciclo de vida
    real do pool. Em falha de conexao, propaga None — as rotas continuam
    respondendo offline_mode/503 de forma graciosa, como ja fazem.
    """
    for _router in _POOL_ROUTERS:
        _router._state.db_pool = db_pool
    # Passo 4.4: o sender de cuidado vive no closure do startup; o router do
    # WhatsApp só o usa no envio pós-confirmação. Sem pool, não há envio.
    if db_pool is not None:
        whatsapp_router._state.send_care_message = _care_sender
    else:
        whatsapp_router._state.send_care_message = None


@app.on_event("startup")
async def startup():
    global db_pool
    if HAS_POSTGRES:
        for attempt in range(1, 6):
            try:
                db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, timeout=10)
                print(f"[PostgreSQL] Pool de conexões assíncrono estabelecido com sucesso na tentativa {attempt}!")
                async with db_pool.acquire() as conn:
                    await conn.execute("SELECT 1;")
                await migrate_legacy_json_devices(db_pool)
                break
            except Exception as e:
                print(f"[PostgreSQL Error] Tentativa {attempt}/5 falhou ao conectar ao banco: {e}")
                if attempt < 5:
                    await asyncio.sleep(3)

        if db_pool:
            await rate_limiter.load_blocked_ips_from_db(db_pool)
            _propagate_db_pool()  # routers extras: pool vivo a partir de agora

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

            try:
                from services.price_cascade import seed_inicial
                await seed_inicial(db_pool)
            except Exception as e:
                print(f"[Startup] Aviso ao rodar seed_inicial de alimentos: {e}")

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
            async def cleanup_deleted_accounts():
                from services.account_deletion import cleanup_account_files
                async with db_pool.acquire() as conn:
                    jobs = await conn.fetch("SELECT user_id FROM account_storage_cleanup ORDER BY created_at LIMIT 100")
                for job in jobs:
                    await cleanup_account_files(db_pool, job["user_id"])
            retention_scheduler.add_job(
                cleanup_deleted_accounts, 'interval', minutes=5,
                id="cleanup_deleted_accounts", replace_existing=True, max_instances=1,
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

            async def _care_sender(phone, message, notification_type):
                if not HAS_WHATSAPP:
                    print("[Care OS] WhatsApp indisponível; entrega não realizada.")
                    return False
                template_env = {
                    "NIGHTLY_BULLETIN": "WHATSAPP_TEMPLATE_CARE_NIGHTLY",
                    "PATTERN_BREAK": "WHATSAPP_TEMPLATE_CARE_PATTERN_BREAK",
                    "STOCK_OR_PRESCRIPTION": "WHATSAPP_TEMPLATE_CARE_STOCK",
                }.get(notification_type)
                template_name = os.getenv(template_env, "") if template_env else ""
                if template_name:
                    result = await send_whatsapp_template(
                        phone, template_name=template_name, language_code="pt_BR",
                        body_parameters=[message],
                    )
                else:
                    # Funciona somente se o usuário abriu a janela Meta de 24h.
                    result = await send_whatsapp_text(phone, message)
                return bool(result.get("success")) if isinstance(result, dict) else bool(result)

            async def _nightly_care_job():
                await send_nightly_bulletins(db_pool, _care_sender)

            async def _pattern_care_job():
                await send_pattern_break_alerts(db_pool, _care_sender)

            async def _stock_care_job():
                await send_stock_alerts(db_pool, _care_sender)

            retention_scheduler.add_job(
                _nightly_care_job, CronTrigger(hour=20, minute=0, timezone="America/Sao_Paulo"),
                id="care_os_nightly_2000_brt", replace_existing=True, max_instances=1, coalesce=True,
            )
            retention_scheduler.add_job(
                _pattern_care_job, 'interval', minutes=30,
                id="care_os_pattern_break", replace_existing=True, max_instances=1, coalesce=True,
            )
            retention_scheduler.add_job(
                _stock_care_job, CronTrigger(hour=9, minute=15, timezone="America/Sao_Paulo"),
                id="care_os_stock_alert", replace_existing=True, max_instances=1, coalesce=True,
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
            #
            # BUG pre-existente (detectado no log da EC2 21/09 14h30): antes os
            # 3 jobs passavam 'lambda: asyncio.create_task(...)' — um callable
            # SINCRONO. O AsyncIOScheduler roda sync callables num executor de
            # thread, onde NAO ha event loop rodando, entao create_task() lia
            # 'RuntimeError: no running event loop' e a rotina caia toda vez.
            # Os outros jobs deste scheduler (purge_*) passam a corrotina
            # direto e funcionam; estes precisam do mesmo tratamento.
            for _h, _m, _periodo, _id in (
                (9, 0, "manha", "proactive_manha"),
                (14, 30, "hidratacao", "proactive_hidratacao"),
                (20, 30, "noite", "proactive_noite"),
            ):
                retention_scheduler.add_job(
                    scheduled_proactive_job, CronTrigger(hour=_h, minute=_m),
                    args=[_periodo], id=_id,
                    replace_existing=True, max_instances=1, coalesce=True,
                )
            async def _price_cascade_job():
                try:
                    from services.price_cascade import atualizar_precos
                    await atualizar_precos(db_pool)
                except Exception as err:
                    print(f"[price_cascade] Falha no job semanal de preços: {err}")

            retention_scheduler.add_job(
                _price_cascade_job,
                CronTrigger(day_of_week="mon", hour=3, minute=0, timezone="America/Sao_Paulo"),
                id="grocery_price_cascade_weekly", replace_existing=True, max_instances=1, coalesce=True,
            )
            print("[Scheduler] Rotinas pró-ativas registradas: 09h00, 14h30, 20h30. Job semanal IPCA registrado: Seg 03h00.")
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
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.token, d.user_id, p.full_name
                FROM device_push_tokens d
                JOIN profiles p ON p.id = d.user_id
                WHERE d.is_active = true
                  AND NOT EXISTS (
                    SELECT 1 FROM vital_signs v
                    WHERE v.user_id = d.user_id
                      AND v.created_at >= NOW() - INTERVAL '4 hours'
                  );
                """
            )

        if not rows or not isinstance(rows, (list, tuple)):
            return

        for r in rows:
            token = r["token"]
            uid = r["user_id"]
            full_name = r["full_name"]
            first_name = f", {full_name.split()[0]}" if full_name and full_name.strip() else ""
            msg = {
                "manha": f"Bom dia{first_name}! Que tal registrar sua pressão agora, logo depois do café?",
                "hidratacao": f"Boa tarde{first_name}! Já bebeu um copo d'água? Vale anotar para fechar o dia bem.",
                "noite": f"Boa noite{first_name}! Fechou o dia? Que tal registrar como você está se sentindo?",
            }.get(periodo, "Hora de cuidar de você.")
            try:
                await asyncio.to_thread(
                    dispatch_push,
                    token=token,
                    title="Lembrete do Braga Saúde",
                    body=msg,
                    data={"type": "PROACTIVE_BRAGA", "period": periodo},
                )
            except Exception as push_err:
                print(f"[Scheduler] Falha no push pró-ativo para {uid}: {push_err}")
    except Exception as e:
        print(f"[Scheduler] Erro na rotina pró-ativa {periodo}: {e}")

@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if globals().get('security_telemetry'):
        await security_telemetry.stop()
    if globals().get("retention_scheduler"):
        retention_scheduler.shutdown(wait=False)
    if db_pool:
        await db_pool.close()
        db_pool = None
        _propagate_db_pool()  # routers extras: pool fechado, volta a None

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
        caregiver_tokens = []
        if pool:
            caregiver_tokens = await get_active_tokens_for_caregivers(pool, exclude_user_id=source_user_id)
        if not caregiver_tokens:
            devices = load_devices()
            for uid, dev in devices.items():
                if dev.get("role") in ("CAREGIVER", "caregiver") and uid != source_user_id and dev.get("token"):
                    caregiver_tokens.append(dev["token"])
        for token in caregiver_tokens:
            try:
                await asyncio.to_thread(
                    dispatch_push,
                    token,
                    "Alerta Clínico Braga Saúde",
                    msg_body,
                    {"type": "ALERT_CRITICAL", "sourceUserId": source_user_id},
                )
            except Exception as push_err:
                print(f"[Clinical Alert] Falha no push para cuidador: {push_err}")
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

# =============================================================
# ROTAS REST DE SINCRONIZACAO POSTGRESQL — ver routers/sync.py
# (Fase A passo 5). 22 endpoints (/api/sync/*, /api/profile/*,
# /api/vitals/*, /api/metrics/*, /api/social/*, /api/feedback/*,
# /api/medications/*, /api/agent/receipt).
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
# MENOS protegidas — a doc de especificacao do app prometia bloqueio de <|im_start|>
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

# Registro do router do WhatsApp (Fase A passo 7), com respostas em texto.
# send_care_message (passo 4.4) é definido abaixo no startup e propagado por
# _propagate_db_pool, junto com o pool — não existe aqui em tempo de import.
whatsapp_router.configure_state(
    db_pool=None,  # preenchido no startup (init_db)
    sem_wa=SEM_WA,
    send_care_message=None,
)
app.include_router(whatsapp_router.router)

# Registro do router admin do portal (D-WS1). Autenticado por
# SERVICE_TOKEN (env), escopo reduzido — ver routers/admin.py.
admin_router.configure_state(
    db_pool=None,  # preenchido no startup por _propagate_db_pool()
    rate_limiter=rate_limiter,
)
app.include_router(admin_router.router)

care_os_router.configure_state(db_pool=None, public_base_url=PUBLIC_API_BASE_URL)
app.include_router(care_os_router.router)

catalog_router.configure_state(db_pool=None)
app.include_router(catalog_router.router)

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
# (transcricao -> router Fase 4 -> Groq), respostas rapidas em texto,
# callback de confirmacao e /api/whatsapp/{status,send,events}.
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

from security_telemetry import install as install_security_telemetry
security_telemetry = install_security_telemetry(app, lambda: db_pool)
admin_router._state.telemetry = security_telemetry

if os.path.isdir(app_router.ONBOARDING_DIR):
    app.mount("/", StaticFiles(directory=app_router.ONBOARDING_DIR, html=True), name="onboarding")
