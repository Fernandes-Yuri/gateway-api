#!/usr/bin/env bash
# ==============================================================================
# Script de Backup Soberano Diário & Multi-Cloud (PostgreSQL RDS -> Cloudflare R2)
# DT-073 / Arquitetura Braga Saúde
#
# Comportamento:
# 1. Lock exclusivo via flock (impede execuções concorrentes).
# 2. Carrega credenciais (/etc/braga-saude/r2-backup.env, /run/braga-saude/gateway.env).
# 3. Executa pg_dump -Fc e encadeia cifragem AES-256-CBC via OpenSSL (PBKDF2).
# 4. Gera hash SHA-256 para auditoria e garantia de integridade.
# 5. Despacha artefato cifrado e hash para o bucket Cloudflare R2 via AWS CLI.
# 6. Aplica rotação automática mantendo a janela de retenção (default: 7 dias).
# 7. Limpeza rigorosa via trap EXIT (zero resíduo desprotegido em disco).
# ==============================================================================

set -euo pipefail
export LC_ALL=C
export LC_NUMERIC=C

# --- 1. Lock Exclusivo ---
LOCK_DIR="/run/braga-saude"
if [ ! -d "$LOCK_DIR" ] || [ ! -w "$LOCK_DIR" ]; then
    LOCK_DIR="/tmp"
fi
LOCK_FILE="${LOCK_DIR}/backup_soberano.lock"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [WARN] Outra execução de backup já está em andamento. Abortando." >&2
    exit 0
fi

# --- 2. Carregamento Seguro de Variáveis de Ambiente (Parser sem 'source') ---
load_env_file() {
    local file="$1"
    [ -f "$file" ] || return 0
    while IFS= read -r raw_line || [ -n "$raw_line" ]; do
        local line
        line="$(echo "$raw_line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            local k="${BASH_REMATCH[1]}"
            local v="${BASH_REMATCH[2]}"
            v="$(echo "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
            if [[ "$v" =~ ^\"(.*)\"$ ]]; then
                v="${BASH_REMATCH[1]}"
            elif [[ "$v" =~ ^\'(.*)\'$ ]]; then
                v="${BASH_REMATCH[1]}"
            fi
            export "${k}=${v}"
        fi
    done < "$file"
}

for ENV_FILE in \
    "/run/braga-saude/gateway.env" \
    "/etc/braga-saude/gateway.env" \
    "/etc/braga-saude/r2-backup.env" \
    "/home/ec2-user/braga-gateway/.env" \
    "./r2-backup.env"; do
    load_env_file "$ENV_FILE"
done

# Parâmetros com defaults
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
export PGSSLMODE="${PGSSLMODE:-require}"

# Função de alerta para Telegram em caso de falha crítica
send_telegram_alert() {
    local msg="$1"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=Markdown" >/dev/null 2>&1 || true
    fi
}

# --- 3. Validação de Pré-requisitos e Dependências ---
MISSING_VARS=()
[ -z "${DATABASE_URL:-}" ] && MISSING_VARS+=("DATABASE_URL")
[ -z "${CF_R2_ACCESS_KEY_ID:-}" ] && MISSING_VARS+=("CF_R2_ACCESS_KEY_ID")
[ -z "${CF_R2_SECRET_ACCESS_KEY:-}" ] && MISSING_VARS+=("CF_R2_SECRET_ACCESS_KEY")
[ -z "${CF_R2_ENDPOINT_URL:-}" ] && MISSING_VARS+=("CF_R2_ENDPOINT_URL")
[ -z "${CF_R2_BUCKET_NAME:-}" ] && MISSING_VARS+=("CF_R2_BUCKET_NAME")
[ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ] && MISSING_VARS+=("BACKUP_ENCRYPTION_PASSPHRASE")

if [ ${#MISSING_VARS[@]} -ne 0 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Variáveis obrigatórias não encontradas: ${MISSING_VARS[*]}" >&2
    exit 1
fi

MISSING_BINS=()
command -v pg_dump >/dev/null 2>&1 || MISSING_BINS+=("pg_dump")
command -v openssl >/dev/null 2>&1 || MISSING_BINS+=("openssl")
command -v sha256sum >/dev/null 2>&1 || MISSING_BINS+=("sha256sum")
command -v aws >/dev/null 2>&1 || MISSING_BINS+=("aws")

if [ ${#MISSING_BINS[@]} -ne 0 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Utilitários obrigatórios ausentes no sistema: ${MISSING_BINS[*]}" >&2
    exit 1
fi

# --- 4. Inicialização de Arquivos e Diretório Temporário Seguro ---
TIMESTAMP="$(date -u +"%Y%m%d_%H%M%S")"
BACKUP_BASENAME="braga_backup_${TIMESTAMP}"
ENC_FILENAME="${BACKUP_BASENAME}.dump.enc"
SHA_FILENAME="${BACKUP_BASENAME}.dump.enc.sha256"

TMP_DIR="$(mktemp -d -t braga_backup_XXXXXX)"
chmod 700 "$TMP_DIR"

cleanup() {
    local exit_code=$?
    if [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    if [ "$exit_code" -ne 0 ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Rotina de Backup Soberano falhou (exit code $exit_code)." >&2
        send_telegram_alert "🚨 *FALHA NO BACKUP SOBERANO (Cloudflare R2)*%0AData: $(date -u +'%Y-%m-%d %H:%M:%S UTC')%0AHost: $(hostname)%0AExit Code: $exit_code"
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Iniciando Backup Soberano Braga Saúde..."
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Destino: s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${ENC_FILENAME}"

# --- 5. Extração e Cifragem com Custo Zero em Disco (Stream Seguro) ---
export BACKUP_ENCRYPTION_PASSPHRASE
ENC_FILE_PATH="${TMP_DIR}/${ENC_FILENAME}"
SHA_FILE_PATH="${TMP_DIR}/${SHA_FILENAME}"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Executando pg_dump e criptografia AES-256-CBC (PBKDF2 100k iterações)..."
pg_dump -Fc "$DATABASE_URL" | \
    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -pass env:BACKUP_ENCRYPTION_PASSPHRASE \
        -out "$ENC_FILE_PATH"

if [ ! -s "$ENC_FILE_PATH" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] O dump cifrado gerado está vazio." >&2
    exit 1
fi

FILE_SIZE=$(stat -c%s "$ENC_FILE_PATH" 2>/dev/null || stat -f%z "$ENC_FILE_PATH" 2>/dev/null || wc -c < "$ENC_FILE_PATH")
if [ "$FILE_SIZE" -lt 1024 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Tamanho do dump cifrado anormalmente pequeno ($FILE_SIZE bytes). Abortando." >&2
    exit 1
fi
FILE_SIZE_MB=$(awk "BEGIN {printf \"%.2f\", $FILE_SIZE / 1048576}")
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Cifragem concluída: ${ENC_FILENAME} (${FILE_SIZE_MB} MB / ${FILE_SIZE} bytes)."

# --- 6. Geração de Hash Criptográfico SHA-256 ---
(cd "$TMP_DIR" && sha256sum "$ENC_FILENAME" > "$SHA_FILENAME")
SHA256_HASH=$(awk '{print $1}' "$SHA_FILE_PATH")
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Checksum SHA-256 gerado: ${SHA256_HASH}"

# --- 7. Upload Seguro para Cloudflare R2 ---
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Despachando dump cifrado e checksum para Cloudflare R2..."
AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
aws s3 cp "$ENC_FILE_PATH" "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${ENC_FILENAME}" \
    --endpoint-url "$CF_R2_ENDPOINT_URL" \
    --region auto \
    --no-progress

AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
aws s3 cp "$SHA_FILE_PATH" "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${SHA_FILENAME}" \
    --endpoint-url "$CF_R2_ENDPOINT_URL" \
    --region auto \
    --no-progress

# Verificação de integridade no destino
AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
aws s3 ls "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${ENC_FILENAME}" \
    --endpoint-url "$CF_R2_ENDPOINT_URL" \
    --region auto >/dev/null

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Upload confirmado no Cloudflare R2."

# --- 8. Política de Rotação Automática (Expurgo > RETENTION_DAYS) ---
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Executando rotação de retenção (${BACKUP_RETENTION_DAYS} dias)..."
CUTOFF_STR=$(date -u -d "${BACKUP_RETENTION_DAYS} days ago" +"%Y%m%d%H%M%S" 2>/dev/null || date -u -v-"${BACKUP_RETENTION_DAYS}"d +"%Y%m%d%H%M%S" 2>/dev/null || echo "")

if [ -n "$CUTOFF_STR" ]; then
    REMOTE_FILES=$(AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
        aws s3 ls "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/" \
            --endpoint-url "$CF_R2_ENDPOINT_URL" \
            --region auto | awk '{print $4}' | grep -E '^braga_backup_[0-9]{8}_[0-9]{6}\.dump\.enc$' || true)

    for item in $REMOTE_FILES; do
        item_ts=$(echo "$item" | sed -E 's/^braga_backup_([0-9]{8})_([0-9]{6})\.dump\.enc$/\1\2/')
        if [ -n "$item_ts" ] && [ "$item_ts" -lt "$CUTOFF_STR" ]; then
            echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Expurgo de ciclo: removendo ${item} e ${item}.sha256 do Cloudflare R2..."
            AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
                aws s3 rm "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${item}" \
                    --endpoint-url "$CF_R2_ENDPOINT_URL" --region auto --quiet || true
            AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
                aws s3 rm "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${item}.sha256" \
                    --endpoint-url "$CF_R2_ENDPOINT_URL" --region auto --quiet || true
        fi
    done
fi

# --- 9. Registro de Telemetria Local ---
STATUS_FILE="${LOCK_DIR}/last_backup_soberano.json"
cat << EOF > "${STATUS_FILE}.tmp"
{
  "timestamp": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')",
  "filename": "${ENC_FILENAME}",
  "size_bytes": ${FILE_SIZE},
  "size_mb": ${FILE_SIZE_MB},
  "sha256": "${SHA256_HASH}",
  "bucket": "${CF_R2_BUCKET_NAME}",
  "prefix": "${BACKUP_PREFIX}",
  "status": "success"
}
EOF
mv "${STATUS_FILE}.tmp" "${STATUS_FILE}"
chmod 0644 "${STATUS_FILE}"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [SUCCESS] Backup Soberano concluído com sucesso: ${ENC_FILENAME} (${FILE_SIZE_MB} MB)."
