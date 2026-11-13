#!/usr/bin/env bash
# ==============================================================================
# Teste Automatizado de Ciclo Completo do Backup Soberano R2
# Valida extração mock, cifragem AES-256-CBC (PBKDF2), sha256 e decriptação.
# ==============================================================================

set -euo pipefail

TEST_DIR=$(mktemp -d -t braga_backup_test_XXXXXX)
cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

MOCK_BIN="$TEST_DIR/bin"
mkdir -p "$MOCK_BIN"

# Mock pg_dump: gera cabeçalho conhecido + preenchimento > 1024 bytes
cat << "EOF" > "$MOCK_BIN/pg_dump"
#!/usr/bin/env bash
echo "PGDUMP_MAGIC_HEADER_BRAGA_SAUDE_VERIFIED"
head -c 2048 /dev/zero
EOF
chmod +x "$MOCK_BIN/pg_dump"

# Mock aws CLI: simula upload s3 cp e verificação s3 ls
cat << "EOF" > "$MOCK_BIN/aws"
#!/usr/bin/env bash
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    DEST_NAME="$(basename "$4")"
    cp "$3" "$CP_DEST/$DEST_NAME"
elif [ "$1" = "s3" ] && [ "$2" = "ls" ]; then
    echo "2026-11-12 14:00:00 2048 braga_backup_20261112_140000.dump.enc"
fi
EOF
chmod +x "$MOCK_BIN/aws"

export CP_DEST="$TEST_DIR"
export PATH="$MOCK_BIN:$PATH"

export DATABASE_URL="postgresql://test:test@localhost:5432/testdb"
export CF_R2_ACCESS_KEY_ID="mock_r2_key"
export CF_R2_SECRET_ACCESS_KEY="mock_r2_secret"
export CF_R2_ENDPOINT_URL="https://mock.r2.cloudflarestorage.com"
export CF_R2_BUCKET_NAME="mock-bucket"
export BACKUP_ENCRYPTION_PASSPHRASE="teste_de_cifragem_soberana_123456"

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/maintenance/backup_soberano_r2.sh"
"$SCRIPT_PATH"

ENC_FILE=$(ls "$TEST_DIR"/braga_backup_*.dump.enc | head -n 1)
SHA_FILE="${ENC_FILE}.sha256"

# 1. Valida integridade do SHA-256
(cd "$TEST_DIR" && sha256sum -c "$(basename "$SHA_FILE")")

# 2. Descriptografa com a mesma chave e valida conteúdo
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass pass:"$BACKUP_ENCRYPTION_PASSPHRASE" \
    -in "$ENC_FILE" -out "$TEST_DIR/decrypted.dump"

if grep -q "PGDUMP_MAGIC_HEADER_BRAGA_SAUDE_VERIFIED" "$TEST_DIR/decrypted.dump"; then
    echo "=== TESTE DE BACKUP SOBERANO (DIÁRIO): SUCESSO ==="
else
    echo "=== TESTE DE BACKUP SOBERANO: FALHA NA RECUPERAÇÃO ===" >&2
    exit 1
fi

# 3. Testa despacho da cópia semanal quando hoje coincide com BACKUP_WEEKLY_DAY
export BACKUP_WEEKLY_DAY="$(date -u +%u)"
"$SCRIPT_PATH"

WEEKLY_FILE=$(ls "$TEST_DIR"/braga_backup_weekly_*.dump.enc | head -n 1 || true)
if [ -n "$WEEKLY_FILE" ] && [ -f "$WEEKLY_FILE" ]; then
    echo "=== TESTE DE BACKUP SOBERANO (SEMANAL GFS): SUCESSO ==="
else
    echo "=== TESTE DE BACKUP SOBERANO: FALHA AO GERAR CÓPIA SEMANAL GFS ===" >&2
    exit 1
fi

echo "=== TESTE DE BACKUP SOBERANO: SUCESSO ABSOLUTO ==="
