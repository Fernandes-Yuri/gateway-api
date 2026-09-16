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

python3.12 -m venv venv
source venv/bin/activate
pip install fastapi "uvicorn[standard]" asyncpg httpx "pydantic>=2" \
  firebase-admin apscheduler onnxruntime aiohttp aiofiles \
  cryptography "python-multipart" Jinja2 espeakng-loader
```

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

```
scripts/server/
├── server.py              # Main FastAPI app (routes, startup, DB pool)
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
├── test_*.py              # Test suite (unittest)
└── persona/               # System prompts
```

## 🔌 Principais Rotas

| Método | Rota | Descrição |
|---|---|---|
| GET | `/health` | Health check + status de todos os serviços |
| GET | `/api/profile/{id}` | Buscar perfil |
| POST | `/api/sync/profile` | Sincronizar perfil |
| POST | `/api/auth/otp/send` | Enviar OTP |
| POST | `/api/auth/otp/verify` | Verificar OTP |
| WebSocket | `/ws/orb` | Chat de voz streaming (Orb) |
| GET | `/api/app/latest` | Última versão do app (OTA) |

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
python -m pytest test_*.py -q
```

## 📚 Documentação Relacionada

- `FASE_A_INVENTARIO_MODULARIZACAO.md` — Inventário completo de rotas e dependências
- `41_GUIA_MIGRACAO_AWS_CONCLUIDA.md` — Guia de deploy na AWS