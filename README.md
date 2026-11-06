# Braga Saúde — Gateway API

> **API Backend de Alta Concorrência para o Ecossistema Braga Saúde (Aplicativo Android)**
> **Arquitetura de Produção em Três Camadas:** Borda Nativa em Rust (`braga-edge` / Axum) + Microsserviço Go de Chat Familiar (`braga-chat`) + Core de Aplicação em Python (`FastAPI` / Gunicorn).
> **Infraestrutura:** AWS EC2 (`t3.small`) + Amazon RDS PostgreSQL (`gp3`) + Cloudflare Tunnel + Groq Cloud (LLMs Clínicos).

---

## 🏗️ Arquitetura do Sistema

O Gateway é composto por três processos especializados, orquestrados por sockets Unix privados e atrás de uma única borda pública:

- **Rust (`braga-edge`)** concentra o tráfego de entrada: health probes, catálogo ANVISA com cache em memória, leituras clínicas autoritativas, escritas de alta frequência, validação JWT RS256 e rate limiting global.
- **Go (`braga-chat`)** é a **SSOT única do chat familiar**: vínculos, mensagens E2EE (Blind Relay), recibos de leitura, tombstoning, retenção de 24h e WebSocket `/ws/family`.
- **Python (`FastAPI`)** permanece com o Care OS, autenticação/OTP, sincronização transacional, consultas médicas, assistente clínico/Orb, push FCM e rotinas de background.

```mermaid
flowchart TD
    App["📱 App Android Braga Saúde"] --> CF["🛡️ Cloudflare Tunnel"]
    CF --> Edge["⚡ Borda Nativa Rust :8080<br/>(braga-edge)"]

    subgraph Borda ["Borda de Alta Performance (Rust / Axum)"]
        Edge --> B1["Health/Ready/Landing + 410 de APK e Áudio"]
        Edge --> B2["Catálogo ANVISA (Cache Moka em Memória)"]
        Edge --> B3["Leituras clínicas + escritas /api/sync/*"]
        Edge --> B4["JWT RS256 + Rate Limiter central"]
    end

    Edge -->|/run/braga-saude/chat.sock| Go["🐹 braga-chat (Go)<br/>chat.sock + 127.0.0.1:8082"]
    Edge -->|/run/braga-saude/backend.sock| Py["🐍 Core Python (FastAPI/Gunicorn)<br/>backend.sock + 127.0.0.1:8083"]

    subgraph Chat ["Chat Familiar (Go)"]
        Go --> G1["Blind Relay E2EE (ECDH + AES-256-GCM)"]
        Go --> G2["WebSocket /ws/family em tempo real"]
        Go --> G3["Recibos de leitura e tombstones (24h)"]
        Go --> G4["Vínculos e chaves públicas"]
    end

    subgraph Backend ["Inteligência & Regras de Negócio (Python)"]
        Py --> P1["Assistente Clínico & LLMs (Groq AI)"]
        Py --> P2["Voz streaming WebSocket /ws/orb"]
        Py --> P3["Care OS, consultas médicas e sync"]
        Py --> P4["Push FCM, e-mails e background jobs"]
    end

    B3 --> DB[("🗄️ PostgreSQL RDS (AWS gp3)<br/>ALE AES-256-GCM + Blind Index")]
    Go --> DB
    Py --> DB
```

### 1. Borda Nativa em Rust (`rust/crates/braga-edge`)
- **Proxy Reverso & Borda HTTP/WebSocket:** termina o tráfego do Cloudflare na porta **8080**, injeta security headers (`nosniff`, `DENY`, CSP restritiva) e encaminha por socket Unix.
- **Catálogo de Medicamentos ANVISA (`GET /api/anvisa/medications/barcode/{ean}`):** validação estrita de 13 dígitos e cache em memória com [`moka`](https://crates.io/crates/moka) (TTL 5 min, LRU 10.000 itens).
- **Leituras Clínicas Autoritativas:** consultas de alto volume (`vitals`, `biometry`, `daily-metrics`, `medications`, `medication-logs`, `activity-feed`) executadas nativamente no RDS, sem passar pelo Python.
- **Escritas de Alta Frequência:** `POST /api/sync/biometry`, `/api/sync/daily-metrics`, `/api/sync/medication`, `/api/sync/medication-log`, `/api/sync/audit-log` e `/api/agent/receipt` (habilitáveis por flags/ambiente).
- **Rotas aposentadas com `410 Gone` na entrada:** distribuição de APK (`/api/app/*`) e áudio legado (`/synthesize_voice`, `/v1/audio/speech`).
- **Identidade & Rate Limiter Único:** validação criptográfica de tokens Firebase RS256 com cache de certificados do Google e janela deslizante compartilhada entre workers.

### 2. Microsserviço Go `braga-chat` (`go/`)
- **Núcleo HTTP com `go-chi/chi/v5`** e repositório PostgreSQL via `jackc/pgx/v5`, com pool estritamente limitado (`max_conns=5`) para proteger o RDS.
- **Blind Relay E2EE:** o servidor nunca decifra o conteúdo — valida o envelope AES-256-GCM e persiste apenas o ciphertext.
- **Tempo real:** hub WebSocket `/ws/family` com heartbeats, isolamento por família e eventos `FAMILY_MESSAGE`, `MESSAGE_DELETED` e `MESSAGE_READ`.
- **Retenção D47:** mensagens e tombstones expiram em 24h; recibos de leitura são persistidos em `is_read`.
- **Vínculos familiares:** busca por código, aceite de convite, publicação de chaves ECDH e listagem de vínculos.
- **Transporte dual:** socket Unix `chat.sock` (produção) e TCP `127.0.0.1:8082` (desenvolvimento/diagnóstico), com graceful shutdown por `SIGINT`/`SIGTERM`.

### 3. Core de Aplicação em Python (`scripts/server/`)
- **Autenticação & Segurança:** OTP, sessões, reCAPTCHA e hardening do portal administrativo.
- **Care OS & Sincronização:** contratos de família/cuidador, farmácia, perfil, sinais vitais, métricas e moderação de conteúdo.
- **Consultas Médicas:** `/api/consultation/*` (agendamento, aceite/recusa/conclusão/cancelamento) e confirmação de rascunho por voz.
- **Assistente Clínico & IA:** Groq Cloud, chat REST (`/v1/chat/completions`) e voz streaming Orb (`/ws/orb`).
- **Background Jobs:** push FCM, e-mails transacionais, retenção, reconciliação de conta e rotinas proativas.

> **Nota (Fase #6):** o chat familiar e o E2EE foram removidos do Python — quem os serve é o Go `braga-chat`. A **única** rota familiar ainda no Python é `POST /api/sync/family-binding` (criação/revogação de convite), que ainda não possui equivalente no Go; ela vive em `routers/sync.py` e está coberta por teste dedicado até ser portada.

---

## 🚀 Quick Start Local

### Pré-requisitos
- **Python 3.12+**
- **Go 1.25+** (`go.mod`)
- **Rust estável** (toolchain fixada em `rust/rust-toolchain.toml`)
- **PostgreSQL 16+** (opcional localmente se usar migrações/harness sintético)

### 1. Backend Python
```bash
cd gateway-api

# Ambiente virtual + dependências de desenvolvimento/CI (com hashes)
python3 -m venv venv
source venv/bin/activate
pip install --require-hashes -r requirements-dev.lock

# Variáveis de ambiente
cp .env.example .env
```
```bash
cd scripts/server
source ../../venv/bin/activate
uvicorn server:app --host 127.0.0.1 --port 8083 --reload
```

> **Documentação Interativa (`/docs`, `/redoc`):** desativada em produção por segurança. Localmente, ative com:
> ```bash
> DEVELOPMENT_MODE=1 uvicorn server:app --host 127.0.0.1 --port 8083
> ```

### 2. Microsserviço Go (`braga-chat`)
```bash
cd go

# Testes e build
go test -v -race ./...
go build -o ../bin/braga-chat ./cmd/braga-chat

# Execução local (socket Unix opcional; TCP :8082 para diagnóstico)
DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/bragasaude" \
LISTEN_ADDR="127.0.0.1:8082" \
../bin/braga-chat
```

### 3. Borda Rust (`braga-edge`)
```bash
cd rust

cargo build --release --bin braga-edge --bin braga-sim

./target/release/braga-edge \
    --host 127.0.0.1 \
    --port 8080 \
    --backend-sock /run/braga-saude/backend.sock \
    --backend-tcp-addr 127.0.0.1:8083 \
    --chat-sock /run/braga-saude/chat.sock
```

---

## 📁 Estrutura do Repositório

```
gateway-api/
├── rust/                                # Workspace Rust (borda + ferramentas)
│   ├── Cargo.toml                       # Workspace de 6 crates
│   ├── rust-toolchain.toml              # Toolchain estável fixada
│   └── crates/
│       ├── braga-contracts/             # DTOs e contratos serializáveis compartilhados
│       ├── braga-crypto/                # ALE AES-256-GCM, Argon2id e Blind Index
│       ├── braga-identity/              # JWT Firebase RS256 e autorização autoritativa
│       ├── braga-limiter/               # Rate limiter centralizado
│       ├── braga-edge/                  # Binário de produção: proxy, catálogo e leituras
│       └── braga-sim/                   # Harness de testes e simulação local de borda
│
├── go/                                  # Microsserviço braga-chat (SSOT do chat familiar)
│   ├── cmd/braga-chat/                  # Entrypoint (socket Unix + TCP, graceful shutdown)
│   ├── internal/domain/                 # Entidades de mensagens e vínculos
│   ├── internal/repository/             # Persistência PostgreSQL (pgx v5)
│   ├── internal/transport/http/         # Router chi, handlers e middleware de auth
│   └── internal/transport/ws/           # Hub WebSocket /ws/family
│
├── scripts/
│   ├── server/                          # Core Python (FastAPI, modularizado)
│   │   ├── server.py                    # Bootstrap e registro dos routers
│   │   ├── routers/                     # Endpoints REST/WS por domínio
│   │   │   ├── auth.py                  # Autenticação, OTP e sessões
│   │   │   ├── care_os.py               # Família, cuidadores, farmácia e receitas
│   │   │   ├── chat.py                  # Chat REST /v1/chat/completions
│   │   │   ├── consultations.py         # Consultas médicas e rascunho por voz
│   │   │   ├── sync.py                  # Sync de perfil, vitais, social e vínculo familiar
│   │   │   ├── health.py                # Health, dataset, devices, push e emergência
│   │   │   ├── admin.py                 # Portal administrativo / KPIs
│   │   │   ├── emergency.py             # Página pública de socorro
│   │   │   └── conteudo.py              # Integridade de conteúdo público
│   │   ├── core/                        # Dispositivos, dataset, prompts e utilidades
│   │   ├── services/                    # Regras de negócio e workers de background
│   │   └── schemas/                     # Contratos Pydantic auxiliares
│   ├── maintenance/                     # Scripts operacionais de manutenção
│   └── rust_migration/                  # Simulação e gates de liberação (G0–G4)
│
├── docs/contracts/                      # Contratos versionados (ex.: doses de medicação)
├── infra/systemd/                       # Unidades systemd dos serviços (edge/chat/gateway)
├── migrations/                          # Migrations PostgreSQL versionadas
├── .github/workflows/                   # CI/CD (Python, Go, Rust, deploy EC2 e portal)
├── requirements.txt                     # Input humano das dependências de runtime
├── requirements.lock                    # Lock com hashes (EC2)
└── requirements-dev.lock                # Lock com hashes (CI)
```

---

## 🔌 Rotas Principais da API

### Borda Rust (`braga-edge`, porta 8080)
| Método | Caminho | Descrição |
|---|---|---|
| **GET** | `/health`, `/ready`, `/api/ready`, `/api/internal/stats` | Probes operacionais e telemetria |
| **GET** | `/` | Landing informativa (canal Google Play) |
| **GET/POST** | `/api/app/latest`, `/api/app/download`, `/api/app/version`, `/api/app/notify-update` | Canal próprio encerrado (`410 Gone`) |
| **POST/GET** | `/synthesize_voice`, `/v1/audio/speech` | Áudio legado aposentado (`410 Gone`) |
| **GET** | `/api/anvisa/medications/barcode/{ean}` | Catálogo ANVISA com cache Moka |
| **GET** | `/api/vitals/{user_id}` | Sinais vitais do titular/cuidador |
| **GET** | `/api/biometry/{user_id}` | Biometria (peso, altura, IMC) |
| **GET** | `/api/metrics/daily/{user_id}` | Métricas diárias (passos, distância, calorias) |
| **GET** | `/api/medications/{user_id}` e `/logs` | Medicamentos e histórico de doses |
| **GET** | `/api/family/patients/{id}/activity-feed` | Trilha de auditoria da família |
| **POST** | `/api/sync/biometry`, `/sync/daily-metrics`, `/sync/medication`, `/sync/medication-log`, `/sync/audit-log`, `/agent/receipt` | Escritas de alta frequência no RDS |

### Microsserviço Go (`braga-chat`)
| Método | Caminho | Descrição |
|---|---|---|
| **WS** | `/ws/family` | Chat familiar em tempo real (E2EE) |
| **POST** | `/api/family/messages/send` e `/api/sync/family-message` | Envio idempotente de mensagem cifrada |
| **POST** | `/api/family/messages/read` | Recibos de leitura + broadcast `MESSAGE_READ` |
| **POST** | `/api/family/message/delete` | Tombstone de exclusão |
| **GET** | `/api/family/messages` e `/api/family/messages/{patientId}` | Histórico (sem mensagens expiradas) |
| **GET** | `/api/family/find-by-code/{code}` | Prévia do convite por código |
| **POST** | `/api/family/accept` | Aceite do convite (vinculação) |
| **POST** | `/api/family/e2ee/public-key` | Publicação de chave pública ECDH |
| **GET** | `/api/family/bindings/{userId}` | Vínculos autorizados do usuário |

### Core Python (`FastAPI`)
| Método | Caminho | Descrição |
|---|---|---|
| **POST** | `/api/auth/otp/send` e `/api/auth/otp/verify` | Autenticação móvel via OTP e emissão de sessão |
| **POST/GET** | `/api/consultation/*` | Criar/aceitar/recusar/concluir/cancelar consultas e confirmar rascunho por voz |
| **POST** | `/api/sync/profile`, `/api/sync/vitals`, `/api/sync/medication`, sociais e feedback | Sincronização e regras de negócio |
| **POST** | `/api/sync/family-binding` | Criação/revogação de convite familiar (rota em transição até o port para o Go) |
| **POST** | `/v1/chat/completions` | Canal conversacional do assistente clínico |
| **WS** | `/ws/orb` | Diálogo por voz streaming (Orb) |
| **GET** | `/socorro` | Página pública de orientação de emergência |

> **Canais aposentados (Fase #6):** supermercado/alimentação e WhatsApp foram removidos do Python — não há mais handlers dedicados a eles (respondem `404`; a borda mantém `410` apenas para APK e áudio). Novos clientes não devem consumir esses caminhos.

---

## 🛡️ Segurança & Conformidade de Dados

- **Segredos Dinâmicos (Zero `.env` em Produção):** chaves e credenciais resolvidas em runtime via **AWS Systems Manager Parameter Store + AWS KMS**, carregadas em memória (`tmpfs`).
- **Application-Level Encryption (ALE):** dados clínicos no RDS protegidos com envelope **AES-256-GCM** e DEKs derivadas via HKDF-SHA256.
- **Blind Indexing Determinístico:** consultas exatas sobre campos confidenciais usam HMAC-SHA256 com salt/pepper dedicado, sem descriptografar linhas.
- **Criptografia Ponta a Ponta (E2EE):** o chat familiar usa ECDH (secp256r1) no cliente Android; o Go `braga-chat` atua estritamente como *Blind Relay*, sem acesso ao texto claro.
- **Mitigação de Abusos:** rate limiter central com bloqueio de IP por 24h após múltiplas violações, protegendo autenticação, OTP e rotas públicas.
- **Hardening de Processo:** serviços systemd com `NoNewPrivileges=true`, `PrivateTmp=true` e `LimitCORE=0` (sem dumps com memória clínica).

---

## 🧪 Testes & Validação de Qualidade

### 1. Lint e testes Python (mesmo comando do CI)
```bash
cd gateway-api
source venv/bin/activate

ruff check scripts/server/ scripts/ migrate.py
PYTHONPATH=scripts/server python -m unittest discover scripts/server/ "test_*.py"
```

### 2. Testes Go (chat familiar)
```bash
cd go
go test -v -race ./...
```

### 3. Testes Rust (borda)
```bash
cd rust
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --verbose
```

### 4. Simulação local leve (gates G0–G4)
```bash
python3 scripts/rust_migration/simulate_local.py --phase 2
```

---

## 🚢 CI/CD & Deploy em Produção

### Workflows (GitHub Actions)
- **`validar-branches.yml`:** ruff + suíte Python (`unittest discover`) + ensaio de migrations em PostgreSQL efêmero.
- **`go-ci.yml`:** `go test -race ./...` + build estático linux/amd64 do `braga-chat` (artefato `braga-chat-binary`).
- **`rust-ci.yml`:** fmt/clippy/testes do workspace + build release de `braga-edge`/`braga-sim` (artefatos binários).
- **`validar-autenticacao-portal.yml`:** regressões do portal administrativo (admin, Argon2 e autenticação).
- **`deploy.yml`:** gate `lint-and-test` + `ensaio-migracoes` e deploy na EC2 somente via `master`.

### Deploy na EC2
- Push em `master` dispara **AWS Systems Manager Run Command** (sem SSH exposto): a EC2 faz `fetch`/`reset --hard`, roda `deploy.sh`, aplica migrations e reinicia o `braga-gateway.service` (FastAPI/Gunicorn em `backend.sock` + `127.0.0.1:8083`).
- O `deploy.sh` sincroniza `scripts/server/` → `app/`, instala o `requirements.lock` com hashes e executa `migrate.py`; ao final valida `/health` antes de declarar sucesso.
- **Binários Go e Rust são entregues como artefatos do CI** e instalados na EC2 por publicação no bucket de deploy + SSM (procedimento operacional separado; não são recompilados pelo `deploy.sh`).

### Serviços em produção (systemd)
| Unidade | Papel |
|---|---|
| `braga-edge.service` | Borda Rust em `0.0.0.0:8080` (túnel Cloudflare) |
| `braga-chat.service` | Chat familiar Go em `chat.sock` (+ `127.0.0.1:8082`) |
| `braga-gateway.service` | Core Python em `backend.sock` (+ `127.0.0.1:8083`) |
| `cloudflared.service` | Túnel Cloudflare para a borda |

### Verificação rápida de saúde
```bash
curl -s https://api.bragasaude.online/health
curl -s https://api.bragasaude.online/ready   # inclui backend_sock, chat_sock e database
```
