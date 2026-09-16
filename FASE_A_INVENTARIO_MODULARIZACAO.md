# Fase A — Inventário do Gateway (modularização, doc 36)

> Data: 15/09/2026. Escopo: `scripts/server/` do `gateway-api`.
> Base: leitura de `server.py` (3347 linhas) + grep de rotas/env/DB/serviços externos.
> Objetivo: fundamentar a Fase B (separar config + isolar Piper, manter endpoints e testes).
> Premissa revisada (doc 40/D44): o banco vai para a AWS (RDS `braga-saude-database`), não fica no notebook. Logo **não há túnel WireGuard para o homelab** — gateway (EC2) e banco (RDS) ficam no mesmo VPC us-east-2.

## 1. Superfície pública (62 rotas)

### `server.py` (~50 rotas)
- **Sync de dados**: `/api/sync/{profile,vitals,daily-metrics,social-post,post-reaction,feedback,exam,exam-item,medication,medication-log,league-membership,audit-log,agent/receipt}`
- **Leitura**: `/api/profile/{uid}`, `/api/vitals/{uid}`, `/api/metrics/daily/{uid}`, `/api/exams/{uid}`, `/api/medications/{uid}`, `/api/files/exams/{uid}/{filename}`
- **Chat/voz (OpenAI-compat)**: `POST /v1/chat/completions`, `POST /synthesize_voice`, `GET+POST /v1/audio/speech`
- **WhatsApp**: `GET+POST /api/whatsapp/webhook`, `GET /api/whatsapp/status`, `POST /api/whatsapp/send`, `GET /api/whatsapp/events`
- **Auth/OTP**: `/api/auth/otp/{send,verify}`, `/api/auth/password/reset`, `/api/auth/welcome-email`, `/api/auth/check-account`
- **Distribuição de app**: `/api/app/{latest,download,version}`, `/api/app/notify-update`
- **Device/notif/emerg**: `/api/device/register`, `/api/devices`, `/api/notify/alert`, `/api/emergency/trigger`
- **Diagnóstico/landing**: `GET /health`, `GET /dataset/{stats,sample}`, `GET /` (landing estática servida pelo FastAPI)

### Módulos (instalam rotas via funções `install_*`)
- `family_api.install_family_routes` — consultas + bindings + mensagens da família
- `caregiver_alerts.install_caregiver_alert_routes` — alerta de saúde do cuidador
- `assistant_preferences.install_preference_routes` — preferências do assistente
- `vip_api.install_vip_routes` — VIP signups (público)
- `ws_orb` — WebSocket `/ws/orb` (voz tempo-real do app)

## 2. Variáveis de ambiente (nomes — valores são secretos)
- **Banco**: `DATABASE_URL`
- **Groq**: `GROQ_API_KEY` | `HF_TOKEN`, `GROQ_MODEL`, `GROQ_MODEL_{CHAT,REST,WHATSAPP}`, `GROQ_API_KEY_{CHAT,WHATSAPP}`
- **Piper**: `PIPER_BIN`, `PIPER_MODELS_DIR`
- **WhatsApp**: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_API_VERSION`, `WHATSAPP_BUSINESS_NUMBER`, `WHATSAPP_VERIFY_TOKEN`
- **Firebase/FCM**: `FIREBASE_SERVICE_ACCOUNT_PATH`
- **E-mail**: `GMAIL_SMTP_{HOST,PORT,USERNAME,APP_PASSWORD}`, `GMAIL_FROM_{EMAIL,NAME}`
- **App/pub**: `PUBLIC_API_BASE_URL`, `BRAGA_RELEASES_DIR`, `BRAGA_PROJECT_ROOT`, `VIP_ALLOWED_ORIGINS`, `TRUST_PROXY_HEADERS`, `DATASET_PATH`, `AUDIO_CACHE_DIR`, `STORAGE_DIR`, `DEVICES_PATH`

## 3. Acesso a banco (asyncpg)
- Driver: `asyncpg` (import defensivo, flag `HAS_POSTGRES`).
- Pool: `db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, timeout=10)` no startup.
- Padrão repetido em ~30 pontos de `server.py`: `if not db_pool: ...; async with db_pool.acquire() as conn: ...`.
- Repasse a módulos: `install_*_routes(app, lambda: db_pool, ...)` — recebem um *provider* de pool (lambda), nunca o pool direto.
- Lifespan: startup cria pool + scheduler; shutdown fecha pool.

## 4. Serviços externos
- **Groq** (httpx): classificação + geração + transcrição (whisper) + speak; semáforos `GROQ_SEMAPHORE(5)`, `SEM_CHAT(3)`/`SEM_REST(2)`/`SEM_WA(3)` (concorrência por canal).
- **Firebase Admin**: FCM (push) + verificação de token (`api_identity.firebase_uid`). Init no momento do import.
- **WhatsApp Cloud API** (`whatsapp_service`): send/template/audio/interactive/list/reaction, download+transcribe mídia.
- **Piper** (`PiperService`): subprocesso local, semáforo interno, voz pt-BR Faber.
- **APScheduler**: `AsyncIOScheduler` + `CronTrigger` (notificações agendadas).
- **Gmail SMTP** (`email_service`): OTP/welcome por e-mail.

## 5. Caminhos hardcoded do homelab (parametrizar para a EC2)
- `PIPER_BIN` default `/home/yurifjs/piper/piper/piper`
- `FIREBASE_SERVICE_ACCOUNT_PATH` default `/home/yurifjs/projetos/braga-gateway/serviceAccountKey.json`
- `PUBLIC_API_BASE_URL` default `https://mashing-spousal-prescribe.ngrok-free.dev` (ngrok legado)
- `DATASET_PATH`, `AUDIO_CACHE_DIR`, `STORAGE_DIR`, `DEVICES_PATH` derivados de `PROJECT_ROOT`.

## 6. Riscos herdados (doc 36 §3) — endereçar na Fase B / antes do deploy público
- POST do webhook WhatsApp valida `verify_token` (no GET), mas **validação de assinatura do corpo bruto não encontrada** → obrigatório antes de abrir origem AWS.
- OTP: `random.randint` (não criptográfico); revisar consumo único + concorrência.
- CORS `allow_origins=["*"]` com `allow_credentials=True` — reverter para origens explícitas antes do deploy público.
- Áudio: gera WAV com MIME `audio/mpeg` — normalizar formato real/MIME/extensão.
- Monolito de 3347 linhas concentra init, rotas, WhatsApp, TTS, OTP e DB.

## 7. Próxima etapa (Fase B) — proposta de extração por bloco
1. `config.py`: centralizar todas as env vars + defaults + paths (eliminar hardcoded do homelab).
2. `bootstrap.py`: composição do app + init Firebase + pool + scheduler (tirar do escopo global de `server.py`).
3. `migrations/`: versionar os SQL `003_whatsapp_confirmations.sql` e `004_agent_actions.sql` (+ criar `001`/`002` de base) e rodar explicitamente em vez de criar tabelas no startup.
4. Isolar Piper em worker com concorrência real + cancelamento + cache atômico (manter o contrato da rota de áudio).
5. Mover rotas para `api/` por domínio mantendo os mesmos paths/respostas; `server.py` vira shim compatível.
- **Critério de conclusão da Fase B**: endpoints e formatos idênticos; testes Python (baseline 157+) passando; nenhuma rota importa `server.py`.

## 8. Para o deploy AWS (pós-Fase B)
- `DATABASE_URL` aponta para o RDS `braga-saude-database` (creds via env/Secrets, nunca em código).
- Tráfego gateway↔RDS privado no mesmo VPC us-east-2 (sem túnel homelab).
- Exposição pública via túnel Cloudflare (`cloudflared`) na EC2 → `bragasaude.online` → `:8080`; fechar 80/443 inbound.
- Voz em WebSocket (`/ws/orb`): testar idle timeout (~100s free) do Cloudflare; se falhar, rotear só esse hostname em modo DNS-only.
- Piper na EC2 t3.micro (1 GB RAM): é o ponto de maior risco de memória — medir antes de prometer capacidade (doc 36 §8).
