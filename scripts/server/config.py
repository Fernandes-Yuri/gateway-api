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
    default_dataset_path = os.path.join(DEFAULT_DATA_DIR, "dataset", "dataset_bragasaude.jsonl")
    default_audio_cache_dir = os.path.join(DEFAULT_CACHE_DIR, "audio")
    default_storage_dir = os.path.join(DEFAULT_DATA_DIR, "storage")
    default_devices_path = os.path.join(DEFAULT_DATA_DIR, "devices", "registered_devices.json")
else:
    # Mantém o comportamento atual para execução local fora da pasta app/.
    default_dataset_path = os.path.join(PROJECT_ROOT, "dataset_bragasaude.jsonl")
    default_audio_cache_dir = os.path.join(PROJECT_ROOT, "audio_cache")
    default_storage_dir = os.path.join(PROJECT_ROOT, "storage")
    default_devices_path = os.path.join(PROJECT_ROOT, "registered_devices.json")

DATASET_PATH = os.getenv("DATASET_PATH", default_dataset_path)
AUDIO_CACHE_DIR = os.getenv("AUDIO_CACHE_DIR", default_audio_cache_dir)
STORAGE_DIR = os.getenv("STORAGE_DIR", default_storage_dir)
STORAGE_EXAMS_DIR = os.path.join(STORAGE_DIR, "exams")
DEVICES_PATH = os.getenv("DEVICES_PATH", default_devices_path)
RELEASES_DIR = os.getenv("BRAGA_RELEASES_DIR", os.path.join(PROJECT_ROOT, "releases"))

# Garante que os diretórios existem (mesmo comportamento do server.py linhas 151-154).
os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
os.makedirs(STORAGE_EXAMS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DEVICES_PATH), exist_ok=True)

# ------------------------------------------------------------------
# Banco de dados (asyncpg). DATABASE_URL deve apontar para o destino
# correto por ambiente:
#   - homelab direto: postgresql://bragasaude_user:***@127.0.0.1:5432/bragasaude
#   - EC2 via túnel : postgresql://bragasaude_user:***@127.0.0.1:<porta_tunnel>/bragasaude
# Nada de credencial hardcoded.
# ------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

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
MODEL_WHATSAPP = os.getenv("GROQ_MODEL_WHATSAPP") or MODEL_ID
# Chaves por serviço (fallback para a chave legada do assistente).
GROQ_API_KEY_CHAT = os.getenv("GROQ_API_KEY_CHAT") or GROQ_API_KEY
GROQ_API_KEY_WHATSAPP = os.getenv("GROQ_API_KEY_WHATSAPP") or GROQ_API_KEY

# ------------------------------------------------------------------
# Piper TTS (subprocesso local). No homelab os defaults abaixo funcionam;
# na EC2, definir PIPER_BIN/PIPER_MODELS_DIR para os caminhos instalados
# no deploy. Espelha server.py linhas 103-104.
# ------------------------------------------------------------------
PIPER_BIN = os.getenv("PIPER_BIN", "/home/yurifjs/piper/piper/piper")
PIPER_MODELS_DIR = os.getenv("PIPER_MODELS_DIR", "/home/yurifjs/piper/models")

# ------------------------------------------------------------------
# Firebase / FCM. A serviceAccountKey fica em arquivo; o path é por
# ambiente (na EC2, definir FIREBASE_SERVICE_ACCOUNT_PATH para o local
# instalado). Espelha server.py linha 82.
# ------------------------------------------------------------------
SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    "/home/yurifjs/projetos/braga-gateway/serviceAccountKey.json",
)

# ------------------------------------------------------------------
# Exposição pública. PUBLIC_API_BASE_URL default legado (ngrok) — na
# EC2, definir para a URL pública do túnel Cloudflare. Espelha linha 105.
# ------------------------------------------------------------------
PUBLIC_API_BASE_URL = os.getenv(
    "PUBLIC_API_BASE_URL", "https://mashing-spousal-prescribe.ngrok-free.dev"
).rstrip("/")
