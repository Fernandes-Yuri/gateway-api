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

```bash
cd scripts/server
python -m pytest -q
# 283 passed, 1 skipped (75 subtests)
# asyncio_mode=auto esta em pytest.ini — testes async soltos rodam nativo
```

> Validação em venv zerada antes de qualquer push (doc 06 §12.6): a suite
# so e considerada verde se rodar contra um `pip install -r requirements.txt`
# fresco, sem caches do ambiente de desenvolvimento.