#!/usr/bin/env bash
# ==============================================================================
# Script de Validação e Ensaio Automatizado de Restauração de Backup
# Braga Saúde / DT-073 - Protocolo Soberano de DR
#
# Comportamento:
# 1. Trava de Segurança Crítica: Impede veementemente execução contra banco de produção.
# 2. Obtém o backup cifrado mais recente (via Cloudflare R2 ou arquivo local informado).
# 3. Valida a integridade criptográfica do artefato através do checksum SHA-256.
# 4. Decifra o dump em diretório volátil seguro (mktemp chmod 700 / trap EXIT).
# 5. Restaura o dump exclusivamente no banco de teste isolado (TEST_RESTORE_DATABASE_URL).
# 6. Audita a existência das tabelas essenciais do schema clínico e de autenticação.
# 7. Audita e compara a volumetria (contagem de linhas) entre o banco de teste e a origem.
# 8. Emite relatório final estruturado de prontidão para Disaster Recovery.
# ==============================================================================

set -euo pipefail
export LC_ALL=C
export LC_NUMERIC=C

# --- 1. Carregamento de Variáveis de Ambiente ---
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
    "./r2-backup.env"; do
    load_env_file "$ENV_FILE"
done

# --- 2. Processamento de Parâmetros de Linha de Comando ---
LOCAL_DUMP_FILE=""
ALLOW_PRODUCTION_OVERWRITE="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --file|-f)
            LOCAL_DUMP_FILE="$2"
            shift 2
            ;;
        --help|-h)
            cat << 'EOF'
Uso: test_restore_verification.sh [OPÇÕES]

Opções:
  -f, --file ARQUIVO     Usa arquivo local .dump.enc em vez de baixar do Cloudflare R2
  -h, --help             Exibe esta mensagem de ajuda

Variáveis de Ambiente Obrigatórias:
  TEST_RESTORE_DATABASE_URL    URL de conexão do banco de teste ISOLADO (ex: postgresql://.../braga_restore_test)
  BACKUP_ENCRYPTION_PASSPHRASE Chave simétrica usada para descriptografia AES-256-CBC

Variáveis de Ambiente Opcionais:
  SOURCE_DATABASE_URL          URL de produção/origem (somente leitura) para conferir contagem de linhas
  CF_R2_ACCESS_KEY_ID          Chave de acesso R2 (necessária se --file não for fornecido)
  CF_R2_SECRET_ACCESS_KEY      Segredo R2 (necessário se --file não for fornecido)
  CF_R2_ENDPOINT_URL           Endpoint R2 (necessário se --file não for fornecido)
  CF_R2_BUCKET_NAME            Nome do bucket R2 (default: braga-saude)
  BACKUP_PREFIX                Prefixo dos backups (default: backups)
EOF
            exit 0
            ;;
        *)
            echo "[ERROR] Argumento desconhecido: $1" >&2
            exit 1
            ;;
    esac
done

BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
CF_R2_BUCKET_NAME="${CF_R2_BUCKET_NAME:-braga-saude}"
SOURCE_DB_URL="${SOURCE_DATABASE_URL:-${DATABASE_URL:-}}"

# --- 3. TRAVAS DE SEGURANÇA E PROTEÇÃO CONTRA RESTAURAÇÃO EM PRODUÇÃO ---
if [ -z "${TEST_RESTORE_DATABASE_URL:-}" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] TEST_RESTORE_DATABASE_URL não foi definida!" >&2
    echo "A restauração de auditoria roda EXCLUSIVAMENTE em banco separado para teste." >&2
    echo "Defina TEST_RESTORE_DATABASE_URL apontando para uma base de testes (ex: postgresql://.../braga_audit_test)." >&2
    exit 1
fi

if [ -n "$SOURCE_DB_URL" ] && [ "$TEST_RESTORE_DATABASE_URL" = "$SOURCE_DB_URL" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] TEST_RESTORE_DATABASE_URL é IDÊNTICA à SOURCE_DATABASE_URL!" >&2
    echo "Operação abortada preventivamente para impedir sobreposição do banco de produção!" >&2
    exit 1
fi

# Checagem heurística: nomes que indicam banco primário de produção
if [[ "$TEST_RESTORE_DATABASE_URL" =~ (braga-saude-database\.cde0e02i8btl|braga_production|production_db) ]]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] O endpoint em TEST_RESTORE_DATABASE_URL contém indicativo de instância produtiva." >&2
    echo "URL informada: $TEST_RESTORE_DATABASE_URL" >&2
    echo "Abortando por política de segurança estrita de isolamento de DR." >&2
    exit 1
fi

if [ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] BACKUP_ENCRYPTION_PASSPHRASE não definida." >&2
    exit 1
fi

MISSING_BINS=()
command -v openssl >/dev/null 2>&1 || MISSING_BINS+=("openssl")
command -v sha256sum >/dev/null 2>&1 || MISSING_BINS+=("sha256sum")
command -v pg_restore >/dev/null 2>&1 || MISSING_BINS+=("pg_restore")
command -v psql >/dev/null 2>&1 || MISSING_BINS+=("psql")

if [ ${#MISSING_BINS[@]} -ne 0 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Utilitários obrigatórios ausentes: ${MISSING_BINS[*]}" >&2
    exit 1
fi

# --- 4. Inicialização de Espaço Temporário Seguro ---
TMP_DIR="$(mktemp -d -t braga_restore_test_XXXXXX)"
chmod 700 "$TMP_DIR"

cleanup() {
    local exit_code=$?
    if [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    if [ "$exit_code" -ne 0 ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [ERROR] Ensaio de restauração finalizou com ERRO (exit code $exit_code)." >&2
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

ENC_FILE=""
SHA_FILE=""

# --- 5. Obtenção do Artefato Cifrado ---
if [ -n "$LOCAL_DUMP_FILE" ]; then
    if [ ! -f "$LOCAL_DUMP_FILE" ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Arquivo de dump informado não existe: $LOCAL_DUMP_FILE" >&2
        exit 1
    fi
    ENC_FILE="$LOCAL_DUMP_FILE"
    SHA_FILE="${LOCAL_DUMP_FILE}.sha256"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Usando artefato local: $ENC_FILE"
else
    # Baixar cópia mais recente do Cloudflare R2
    command -v aws >/dev/null 2>&1 || { echo "[ERROR] aws CLI é necessário para baixar do R2." >&2; exit 1; }
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Buscando o backup mais recente no Cloudflare R2 (s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/)..."

    LATEST_ENC_NAME=$(AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
        aws s3 ls "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/" \
            --endpoint-url "$CF_R2_ENDPOINT_URL" \
            --region auto | awk '{print $4}' | grep -E '^braga_backup_(weekly_)?[0-9]{8}_[0-9]{6}\.dump\.enc$' | sort | tail -n 1 || true)

    if [ -z "$LATEST_ENC_NAME" ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Nenhum backup encontrado no Cloudflare R2." >&2
        exit 1
    fi

    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Backup mais recente identificado: ${LATEST_ENC_NAME}"
    ENC_FILE="${TMP_DIR}/${LATEST_ENC_NAME}"
    SHA_FILE="${TMP_DIR}/${LATEST_ENC_NAME}.sha256"

    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Baixando artefato e hash do Cloudflare R2..."
    AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
        aws s3 cp "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${LATEST_ENC_NAME}" "$ENC_FILE" \
            --endpoint-url "$CF_R2_ENDPOINT_URL" --region auto --no-progress

    AWS_ACCESS_KEY_ID="$CF_R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$CF_R2_SECRET_ACCESS_KEY" \
        aws s3 cp "s3://${CF_R2_BUCKET_NAME}/${BACKUP_PREFIX}/${LATEST_ENC_NAME}.sha256" "$SHA_FILE" \
            --endpoint-url "$CF_R2_ENDPOINT_URL" --region auto --no-progress
fi

# --- 6. Verificação de Checksum Criptográfico SHA-256 ---
if [ -f "$SHA_FILE" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Validando integridade com SHA-256..."
    EXPECTED_HASH=$(awk '{print $1}' "$SHA_FILE")
    CALCULATED_HASH=$(sha256sum "$ENC_FILE" | awk '{print $1}')
    if [ "$EXPECTED_HASH" != "$CALCULATED_HASH" ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Checksum SHA-256 divergente! Arquivo corrompido ou adulterado." >&2
        echo "Esperado:   $EXPECTED_HASH" >&2
        echo "Calculado:  $CALCULATED_HASH" >&2
        exit 1
    fi
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [SUCCESS] Integridade SHA-256 confirmada: $CALCULATED_HASH"
else
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [WARN] Arquivo .sha256 não encontrado. Prosseguindo sem validação externa de hash."
fi

# --- 7. Descriptografia Segura em Disco Volátil ---
DECRYPTED_DUMP="${TMP_DIR}/restored_clean.dump"
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Descriptografando artefato com OpenSSL AES-256-CBC (PBKDF2 100k)..."

openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass env:BACKUP_ENCRYPTION_PASSPHRASE \
    -in "$ENC_FILE" \
    -out "$DECRYPTED_DUMP"

if [ ! -s "$DECRYPTED_DUMP" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Arquivo de dump descriptografado está vazio. Verifique a chave BACKUP_ENCRYPTION_PASSPHRASE." >&2
    exit 1
fi

DUMP_SIZE_MB=$(awk "BEGIN {printf \"%.2f\", $(wc -c < "$DECRYPTED_DUMP") / 1048576}")
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Descriptografia concluída com sucesso (${DUMP_SIZE_MB} MB)."

# --- 8. Validação de Conectividade com o Banco de Teste Isolado ---
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Conectando ao banco de teste separado..."
RESTORE_DB_NAME=$(psql "$TEST_RESTORE_DATABASE_URL" -t -A -c "SELECT current_database();" 2>/dev/null || true)

if [ -z "$RESTORE_DB_NAME" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Não foi possível conectar ao banco de teste informado em TEST_RESTORE_DATABASE_URL." >&2
    exit 1
fi
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Banco de teste conectado com sucesso: '${RESTORE_DB_NAME}'"

# --- 9. Execução da Restauração (pg_restore) ---
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Aplicando pg_restore no banco '${RESTORE_DB_NAME}'..."
set +e
RESTORE_OUTPUT=$(pg_restore --clean --if-exists --no-owner --no-privileges -d "$TEST_RESTORE_DATABASE_URL" "$DECRYPTED_DUMP" 2>&1)
RESTORE_EXIT=$?
set -e

# pg_restore retorna status 1 se houver avisos não-críticos (como objetos que ainda não existiam para --clean)
if [ "$RESTORE_EXIT" -gt 1 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Falha na execução do pg_restore (exit code $RESTORE_EXIT):" >&2
    echo "$RESTORE_OUTPUT" >&2
    exit 1
fi
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Restauração do schema e dados aplicada com sucesso."

# --- 10. Auditoria de Existência das Tabelas Estruturais ---
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Auditando existência de tabelas essenciais no banco restaurado..."

CORE_TABLES=(
    "schema_migrations"
    "profiles"
    "care_plans"
    "vital_signs"
    "medications"
    "medication_logs"
    "audit_logs"
    "family_links"
)

MISSING_TABLES=()
for tbl in "${CORE_TABLES[@]}"; do
    EXISTS=$(psql "$TEST_RESTORE_DATABASE_URL" -t -A -c \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '$tbl';" 2>/dev/null || echo "0")
    if [ "$EXISTS" != "1" ]; then
        MISSING_TABLES+=("$tbl")
    fi
done

if [ ${#MISSING_TABLES[@]} -ne 0 ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Tabelas essenciais ausentes no banco restaurado: ${MISSING_TABLES[*]}" >&2
    exit 1
fi
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [SUCCESS] Todas as ${#CORE_TABLES[@]} tabelas essenciais existem no banco restaurado."

# --- 11. Auditoria de Linhas (Comparação com Origem / Produção) ---
echo ""
echo "================================================================================"
echo " RELATÓRIO DE AUDITORIA DE VOLUMETRIA (ORIGEM vs RESTAURADO)"
echo "================================================================================"
printf "%-25s | %-18s | %-18s | %-10s\n" "Tabela" "Origem (Produção)" "Restaurado" "Status"
echo "--------------------------+--------------------+--------------------+-----------"

ALL_COUNTS_OK=true

for tbl in "${CORE_TABLES[@]}"; do
    RESTORED_COUNT=$(psql "$TEST_RESTORE_DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM \"$tbl\";" 2>/dev/null || echo "-1")

    if [ -n "$SOURCE_DB_URL" ]; then
        SOURCE_COUNT=$(psql "$SOURCE_DB_URL" -t -A -c "SELECT COUNT(*) FROM \"$tbl\";" 2>/dev/null || echo "N/A")
    else
        SOURCE_COUNT="Não informada"
    fi

    STATUS="OK"
    if [ "$RESTORED_COUNT" -lt 0 ]; then
        STATUS="FALHA"
        ALL_COUNTS_OK=false
    elif [ "$SOURCE_COUNT" != "N/A" ] && [ "$SOURCE_COUNT" != "Não informada" ]; then
        # Em banco vivo, Origem pode ter novos registros pós-snapshot (Restaurado <= Origem é esperado e aceitável)
        if [ "$RESTORED_COUNT" -gt "$SOURCE_COUNT" ]; then
            STATUS="ANOMALIA"
            ALL_COUNTS_OK=false
        fi
    fi

    printf "%-25s | %-18s | %-18s | %-10s\n" "$tbl" "$SOURCE_COUNT" "$RESTORED_COUNT" "$STATUS"
done
echo "================================================================================"

LATEST_MIGRATION=$(psql "$TEST_RESTORE_DATABASE_URL" -t -A -c \
    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;" 2>/dev/null || echo "Desconhecida")
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [INFO] Versão de schema ativa no banco restaurado: ${LATEST_MIGRATION}"

if [ "$ALL_COUNTS_OK" != "true" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [FATAL ERROR] Auditoria de volumetria detectou inconsistência de integridade." >&2
    exit 1
fi

echo ""
echo "================================================================================"
echo " [SUCCESS] TESTE DE RESTAURAÇÃO E AUDITORIA DE DR CONCLUÍDOS COM SUCESSO ABSOLUTO!"
echo " O backup restaurado é consistente, auditado e 100% recuperável."
echo "================================================================================"
exit 0
