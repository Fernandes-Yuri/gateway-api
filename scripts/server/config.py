"""Configuração central do gateway Braga Saúde — Fase B, passo 1 (doc 36).

Centraliza todas as variáveis de ambiente, defaults e paths que estavam
espalhados no topo do `server.py` (linhas 82-150 + RELEASES_DIR). O
objetivo é eliminar os caminhos hardcoded do homelab para que o gateway
rode em qualquer host (homelab ou EC2) SOMENTE via variáveis de ambiente.

Regra de segurança: valores secretos (chaves, tokens, senhas) vêm
EXCLUSIVAMENTE de variáveis de ambiente — nunca hardcoded aqui. Os
defaults abaixo são apenas paths/padrões não sensíveis.

Este módulo é uma extração comportamentalmente idêntica ao trecho
original do `server.py`; a religação (importar de `config` em vez de
definir inline) e os testes vêm no passo seguinte.
"""
import os

from secrets_loader import (
    AWS_SSM_PREFIX,
    USE_AWS_SECRETS,
    get_secret,
    load_secrets,
)

# SPEC-11: Carrega segredos na inicializacao do processo (AWS SSM Parameter Store
# + KMS em producao quando USE_AWS_SECRETS=1; fallback para .env / os.environ
# em desenvolvimento local e CI).
load_secrets()

# ------------------------------------------------------------------
# Diretórios do projeto (parametrizáveis; defaults relativos ao app)
# Espelha server.py linhas 125-150.
# ------------------------------------------------------------------
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
DEVICES_PATH = os.getenv("DEVICES_PATH", default_devices_path)
RELEASES_DIR = os.getenv("BRAGA_RELEASES_DIR", os.path.join(PROJECT_ROOT, "releases"))

# Garante que os diretórios existem (mesmo comportamento do server.py linhas 151-154).
os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DEVICES_PATH), exist_ok=True)

# ------------------------------------------------------------------
# Banco de dados (asyncpg). DATABASE_URL deve apontar para o destino
# correto por ambiente:
#   - homelab direto: postgresql://bragasaude_user:***@127.0.0.1:5432/bragasaude
#   - EC2 via túnel : postgresql://bragasaude_user:***@127.0.0.1:<porta_tunnel>/bragasaude
# Nada de credencial hardcoded.
# ------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATIONS_DATABASE_URL = os.getenv("MIGRATIONS_DATABASE_URL") or DATABASE_URL

# ------------------------------------------------------------------
# Segredos administrativos e criptograficos (SPEC-10 / SPEC-11).
# Em producao na EC2 vem do AWS SSM Parameter Store (/braga-saude/production/*)
# descriptografados via KMS direto em memoria.
# ------------------------------------------------------------------
AUTH_PEPPER_SECRET = os.getenv("AUTH_PEPPER_SECRET")
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN")
ADMIN_TOTP_SECRET = os.getenv("ADMIN_TOTP_SECRET")
DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY")
BLIND_INDEX_PEPPER = os.getenv("BLIND_INDEX_PEPPER")

# ------------------------------------------------------------------
# Groq (geração + transcrição whisper + speak). Modelos/chaves por
# serviço (cota é por modelo). Espelha server.py linhas 93-102.
# ------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("HF_TOKEN")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_ID = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
# Modelo por serviço (cota é por modelo: cada um tem seu bucket de RPM/RPD/TPM).
MODEL_CHAT = os.getenv("GROQ_MODEL_CHAT") or MODEL_ID
MODEL_REST = os.getenv("GROQ_MODEL_REST") or MODEL_ID
GROQ_MODEL_VISION = os.getenv("GROQ_MODEL_VISION", "llama-3.2-11b-vision-instant")
# Chaves por serviço (fallback para a chave legada do assistente).
GROQ_API_KEY_CHAT = os.getenv("GROQ_API_KEY_CHAT") or GROQ_API_KEY

# ------------------------------------------------------------------
# Google Gemini (Visão Multimodal para Receitas Médicas).
# Endpoint REST oficial do Google AI Studio; usa Gemini 1.5 Flash.
# ------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_VISION = os.getenv("GEMINI_MODEL_VISION", "gemini-1.5-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_VISION}:generateContent"


# ------------------------------------------------------------------
# Firebase / FCM. A serviceAccountKey fica em arquivo; o path vem do env
# (na EC2, FIREBASE_SERVICE_ACCOUNT_PATH aponta para o local instalado).
# Sem default do homelab pela mesma razao acima.
# ------------------------------------------------------------------
SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")

# ------------------------------------------------------------------
# Exposicao publica. PUBLIC_API_BASE_URL deve vir do env (na EC2, a URL
# publica do tunel Cloudflare). O default legado do ngrok foi removido --
# era um endereco morto que so confundia ao portar a infra.
# ------------------------------------------------------------------
PUBLIC_API_BASE_URL = (os.getenv("PUBLIC_API_BASE_URL") or "").rstrip("/")

# ------------------------------------------------------------------
# Modo desenvolvimento. Desliga as protecoes de "superficie zero" que
# so fazem sentido em producao: hoje, /docs, /redoc e /openapi.json
# (AUD-SEC/P2, contrarrrelatorio doc 07 §4 V5). Default fechado —
# producao nunca deve exporta-los; localmente, DEVELOPMENT_MODE=1 os
# reabilita para depuracao.
# ------------------------------------------------------------------
DEVELOPMENT_MODE = os.getenv("DEVELOPMENT_MODE", "0").strip() in ("1", "true", "True", "yes")

# ------------------------------------------------------------------
# Avisos de configuracao obrigatoria ausente. Falha alto e visivel no
# startup para que um ambiente mal configurado seja detectado na hora,
# em vez de virar um bug silencioso em runtime.
# ------------------------------------------------------------------
_REQUIRED_ENV_HINTS = [
    ("FIREBASE_SERVICE_ACCOUNT_PATH", SERVICE_ACCOUNT_PATH, "serviceAccountKey do Firebase"),
    ("PUBLIC_API_BASE_URL", PUBLIC_API_BASE_URL, "URL publica da API"),
    ("DATABASE_URL", DATABASE_URL, "string de conexao do PostgreSQL"),
]
for _name, _value, _desc in _REQUIRED_ENV_HINTS:
    if not _value:
        print(
            f"[config] AVISO: {_name} nao definido no ambiente ({_desc}). "
            f"Defina no .env antes de subir o servico."
        )
del _name, _value, _desc, _REQUIRED_ENV_HINTS

# ------------------------------------------------------------------
# Google reCAPTCHA (validacao server-side do token emitido pelo front).
# RECAPTCHA_SECRET vem exclusivamente de variavel de ambiente.
# Se nao definida, a verificacao e desativada com aviso (nunca aprovada).
# ------------------------------------------------------------------
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")
RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


def reload_secrets(*, force_reload: bool = True, override: bool = True) -> dict[str, str]:
    """Recarrega segredos via secrets_loader e atualiza as variaveis do modulo."""
    global DATABASE_URL, MIGRATIONS_DATABASE_URL, AUTH_PEPPER_SECRET
    global SERVICE_TOKEN, ADMIN_TOTP_SECRET, GROQ_API_KEY, GROQ_API_KEY_CHAT
    global GEMINI_API_KEY, SERVICE_ACCOUNT_PATH, PUBLIC_API_BASE_URL, RECAPTCHA_SECRET
    global USE_AWS_SECRETS, AWS_SSM_PREFIX, DATA_ENCRYPTION_KEY, BLIND_INDEX_PEPPER

    loaded = load_secrets(force_reload=force_reload, override=override)
    import secrets_loader as _sl

    USE_AWS_SECRETS = _sl.USE_AWS_SECRETS
    AWS_SSM_PREFIX = _sl.AWS_SSM_PREFIX
    DATABASE_URL = os.getenv("DATABASE_URL")
    MIGRATIONS_DATABASE_URL = os.getenv("MIGRATIONS_DATABASE_URL") or DATABASE_URL
    AUTH_PEPPER_SECRET = os.getenv("AUTH_PEPPER_SECRET")
    SERVICE_TOKEN = os.getenv("SERVICE_TOKEN")
    ADMIN_TOTP_SECRET = os.getenv("ADMIN_TOTP_SECRET")
    DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY")
    BLIND_INDEX_PEPPER = os.getenv("BLIND_INDEX_PEPPER")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("HF_TOKEN")
    GROQ_API_KEY_CHAT = os.getenv("GROQ_API_KEY_CHAT") or GROQ_API_KEY
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
    PUBLIC_API_BASE_URL = (os.getenv("PUBLIC_API_BASE_URL") or "").rstrip("/")
    RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")
    return loaded


__all__ = [
    "ADMIN_TOTP_SECRET",
    "APP_DIR",
    "AUDIO_CACHE_DIR",
    "AUTH_PEPPER_SECRET",
    "AWS_SSM_PREFIX",
    "BLIND_INDEX_PEPPER",
    "DATABASE_URL",
    "DATASET_PATH",
    "DATA_ENCRYPTION_KEY",
    "DEVELOPMENT_MODE",
    "DEVICES_PATH",
    "GEMINI_API_KEY",
    "GEMINI_API_URL",
    "GEMINI_MODEL_VISION",
    "GROQ_API_KEY",
    "GROQ_API_KEY_CHAT",
    "GROQ_API_URL",
    "GROQ_MODEL_VISION",
    "MIGRATIONS_DATABASE_URL",
    "MODEL_CHAT",
    "MODEL_ID",
    "MODEL_REST",
    "PROJECT_ROOT",
    "PUBLIC_API_BASE_URL",
    "RECAPTCHA_SECRET",
    "RECAPTCHA_VERIFY_URL",
    "RELEASES_DIR",
    "SERVICE_ACCOUNT_PATH",
    "SERVICE_TOKEN",
    "STORAGE_DIR",
    "USES_ORGANIZED_LAYOUT",
    "USE_AWS_SECRETS",
    "get_secret",
    "load_secrets",
    "reload_secrets",
]
