# Braga Saúde — Gateway API

> Backend principal do assistente de saúde por voz Braga Saúde.  
> FastAPI + PostgreSQL + Groq AI + Piper TTS + WhatsApp Cloud API.

## 🏗️ Tech Stack

- **Python 3.12+** (requer `str | None` syntax — PEP 604)
- **FastAPI** (async REST + WebSocket)
- **PostgreSQL** via `asyncpg` (connection pool)
- **Groq Cloud** (LLM: qwen/qwen3.8-27b)
- **Piper TTS** (síntese de voz neural pt-BR, modelos ONNX)
- **Firebase Admin** (FCM push + autenticação)
- **WhatsApp Cloud API** (mensageria)
- **Gmail SMTP** (emails de verificação/recuperação)

## 🚀 Quick Start

### Pré-requisitos
- Python 3.12+
- PostgreSQL 16+
- Piper TTS binary + modelos pt-BR
- chave de API Groq
- conta Firebase + serviceAccountKey.json
- conta WhatsApp Business API

### Setup

```bash
cd gateway-api
cp .env.example .env
# Edite .env com suas chaves

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt   # versões pinadas — não instale à mão
```

> Sem `requirements.txt` as versões divergem entre dev/homelab/EC2 e a
> API quebra por incompatibilidade silenciosa de FastAPI/Pydantic.
> É a primeira coisa que o deploy da EC2 roda.

### Rodar

```bash
cd scripts/server
source ../../venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8080
```

### Health Check

```bash
curl http://localhost:8080/health
# {"status":"online","model":"qwen/qwen3.8-27b",...}
```

### Documentação interativa (`/docs`, `/redoc`, `/openapi.json`)

**Desligada em produção por padrão** (AUD-SEC/P2 — contrarrrelatório de
segurança, doc `07_SEGURANCA.../07_CONTRARRELATORIO_BORDA_E_INFRA.md` §4 V5).
O OpenAPI público entregava um mapa de 74 rotas — incluindo `/api/admin/*` —
como reconhecimento gratuito.

Localmente, reabilite com uma variável de ambiente:

```bash
DEVELOPMENT_MODE=1 uvicorn server:app --host 0.0.0.0 --port 8080
# agora /docs, /redoc e /openapi.json respondem
```

O default é fechado: se `DEVELOPMENT_MODE` não estiver definido, os três
endpoints retornam 404. **Nunca defina `DEVELOPMENT_MODE=1` na EC2.**

## 📁 Estrutura

Monolito modularizado por domínio (Fase A, set/2026 — ver doc
`11_PLANO_MODULARIZACAO_E_TTS.md` no Contexto). O `server.py` era um
arquivo só de 4.123 linhas; hoje é um orquestrador magro (~800) e cada
domínio é um router `core/`/`routers/` com estado injetado via
`configure_state()` — nada de globais cruzando módulos.

```
scripts/server/
├── server.py              # Orquestrador: config, middleware, startup/shutdown,
│                          #   scheduler, dispatch_clinical_alert
├── routers/               # Rotas FastAPI por domínio (APIRouter)
│   ├── health.py          #   /health + status de serviços
│   ├── app.py             #   /api/app/* (OTA, devices, FCM)
│   ├── audio.py           #   /api/audio/* (TTS streaming)
│   ├── chat.py            #   /v1/chat/completions (canal REST)
│   ├── auth.py            #   /api/auth/* (OTP, cadastro, sessão)
│   ├── sync.py            #   /api/sync/* (22 endpoints, gravar/sincronizar)
│   └── whatsapp.py        #   /api/whatsapp/* (webhook, send, events)
├── routers/admin.py       #   /api/admin/* — portal do webservice (D-WS1):
│                          #     token de serviço + métricas aggregate-only
├── core/                  # Lógica de domínio compartilhada
│   ├── text.py            #   Limpeza de texto + mascaramento PII
│   ├── devices.py         #   FCM push + cadastro de dispositivos
│   ├── prompt.py          #   System prompt base
│   ├── confirmation.py    #   Confirmações pendentes (idempotente)
│   └── dataset.py         #   Dataset de fine-tuning
├── config.py              # Centralized env vars & paths
├── orb_router.py          # Voice assistant routes (Orb)
├── ws_orb.py              # WebSocket handler (streaming TTS)
├── piper_service.py       # Piper TTS wrapper
├── whatsapp_service.py    # WhatsApp Cloud API client
├── email_service.py       # Gmail SMTP sender
├── assistant_preferences.py
├── family_api.py
├── caregiver_alerts.py
├── vip_api.py
├── api_identity.py
├── conversation_policy.py
├── rate_limiter.py
├── profile_contract.py
├── trace_llm.py
├── test_*.py              # Test suite (pytest, asyncio_mode=auto)
└── persona/               # System prompts
```

> **Adicionar uma rota nova?** Crie ou edite um arquivo em `routers/` —
> o `server.py` só deve mudar se for ligar um novo domínio no
> `include_router()`. Estado compartilhado vem por `configure_state()`,
> nunca por import direto de global.

## 🔌 Principais Rotas

| Método | Rota | Router | Descrição |
|---|---|---|---|
| GET | `/health` | `health` | Health check + status de todos os serviços |
| GET | `/api/profile/{id}` | `sync` | Buscar perfil |
| POST | `/api/sync/profile` | `sync` | Sincronizar perfil |
| POST | `/api/sync/vitals` | `sync` | Gravar sinais vitais (gatilho clínico) |
| POST | `/api/auth/otp/send` | `auth` | Enviar OTP |
| POST | `/api/auth/otp/verify` | `auth` | Verificar OTP |
| POST | `/v1/chat/completions` | `chat` | Chat conversacional (canal REST) |
| WebSocket | `/ws/orb` | — | Chat de voz streaming (Orb) |
| GET | `/api/app/latest` | `app` | Última versão do app (OTA) |
| GET/POST | `/api/whatsapp/webhook` | `whatsapp` | Webhook da WhatsApp Cloud API |
| POST | `/api/feedback/reply` | `sync` | Resposta a feedback (suporte) |

> 48 rotas no total. Domínio completo de cada uma está no `routers/<dominio>.py`.

### 🔐 Domínio administrativo (D-WS1)

O portal Next.js no Render consome estes endpoints com um **token de serviço**
(`SERVICE_TOKEN` do env — escopo reduzido, não reutiliza `ADMIN_API_TOKEN`).
Nenhuma credencial de banco vai para o Render; o RDS permanece privado.

| Método | Rota | Descrição |
|---|---|---|
| GET | `/api/admin/feedback` | Lista paginada (filtros: `category`, `status`, `priority`, `search`; KPIs em `stats`) |
| PATCH | `/api/admin/feedback/{id}/status` | novo → investigando → resolvido → arquivado (allowlist) |
| POST | `/api/admin/feedback/{id}/reply` | Resposta da equipe → `feedback_replies` (o app lê e notifica) |
| GET | `/api/admin/blocked-ips` | Blocklist autoritativa (`security_blocked_ips`) |
| GET | `/api/admin/metrics/active-users` | `?days=7` — COUNT(DISTINCT user_id) em `audit_logs` |
| GET | `/api/admin/metrics/engagement` | Distribuição de streak e nível por faixa |
| GET | `/api/admin/metrics/notifications` | Opt-in + ações por canal (whatsapp/fcm/app) |
| GET | `/api/admin/metrics/feature-usage` | Top ações registradas (`?days=30`) |

```bash
# Exemplo: métricas de usuários ativos
curl -H "Authorization: Bearer $SERVICE_TOKEN" \
     https://api.bragasaude.online/api/admin/metrics/active-users?days=7
# {"success": true, "activeUsers": 42, "totalUsers": 100, "days": 7}

# Exemplo: feedbacks com KPIs do cabeçalho
curl -H "Authorization: Bearer $SERVICE_TOKEN" \
     "https://api.bragasaude.online/api/admin/feedback?priority=high"
# {"success":true,"data":[...],"pagination":{...},
#  "stats":{"total":12,"pending":3,"highPriority":2,"suggestions":4,"bugs":5,...}}
```

**Autenticação:** `require_service_token` usa `secrets.compare_digest` (constante
no tempo). Token ausente/divergente → **401**; token não configurado no env →
**503** (o recurso nunca fica aberto por default). Cada acesso é logado com path
e IP — o token em si nunca aparece no log.

**Prioridade (temporário):** o gateway ainda não tem `is_high_priority` na tabela
`feedbacks` (D-WS3/migration 014 pendente); o filtro `priority=high` espelha os
16 termos críticos do portal (`socorro`, `tela preta`, `desmaiou`…) via
`LOWER(message) LIKE ANY(...)`. Resultado equivalente até a migration existir.

**Métricas são aggregate-only (k-anonimato):** só números cruzam a rede —
nunca uma linha de usuário. Buckets com menos de 5 usuários são suprimidos
dentro do SQL do gateway, e `audit_logs.metadata` nunca é selecionado.

## 🚢 Deploy (Production)

### Systemd Service

```ini
[Unit]
Description=Braga Saude AI Gateway
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/path/to/braga-gateway/app
EnvironmentFile=/path/to/braga-gateway/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/path/to/braga-gateway/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### Cloudflare Tunnel (HTTPS)

```bash
cloudflared tunnel create braga-gateway
cloudflared tunnel route dns braga-gateway your-domain.com
cloudflared service install <TOKEN>
```

### Piper TTS Setup

```bash
# Binary
wget https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz
tar -xzf piper_linux_x86_64.tar.gz

# PT-BR model (faber-medium)
mkdir -p piper/models
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx \
  -O piper/models/pt_BR-faber-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json \
  -O piper/models/pt_BR-faber-medium.onnx.json
```

## 🔒 Segurança

- `.env` NUNCA versionado — use `.env.example`
- `serviceAccountKey.json` no `.gitignore`
- RDS com `PubliclyAccessible: false`
- Cloudflare WAF + DDoS + HTTPS automático
- `TRUST_PROXY_HEADERS=true` atrás de proxy reverso
- Rate limiting nas rotas sensíveis (OTP, auth)

## 🧪 Testes

### Referências clínicas

`GET /api/clinical/references` entrega as dez faixas canônicas de SBC, SBD,
ADA e OMS para usuários autenticados. A resposta inclui instituição, versão do
documento e parâmetro clínico; os filtros opcionais são `category` e `item_key`.
A migration `016_clinical_references_sources.sql` atualiza registros existentes
de forma idempotente e mantém a fonte rastreável para Android, portal e WhatsApp.

```bash
cd scripts/server
python -m pytest -q
# 283 passed, 1 skipped (75 subtests)
# asyncio_mode=auto esta em pytest.ini — testes async soltos rodam nativo
```

> Validação em venv zerada antes de qualquer push (doc 06 §12.6): a suite
# so e considerada verde se rodar contra um `pip install -r requirements.txt`
# fresco, sem caches do ambiente de desenvolvimento.
