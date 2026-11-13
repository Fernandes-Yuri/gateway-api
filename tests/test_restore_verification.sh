#!/usr/bin/env bash
# ==============================================================================
# Suíte de Testes Automatizados para Validação de Restauração de DR
# Testa travas de segurança contra produção, integridade SHA-256, descriptografia
# e fluxo de restauração em banco isolado.
# ==============================================================================

set -euo pipefail

TEST_DIR=$(mktemp -d -t braga_restore_test_runner_XXXXXX)
cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

MOCK_BIN="$TEST_DIR/bin"
mkdir -p "$MOCK_BIN"

SCRIPT_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/maintenance/test_restore_verification.sh"

PASSPHRASE="teste_de_senha_dr_super_secreta_12345"
MOCK_DUMP_PLAIN="$TEST_DIR/plain_data.dump"
MOCK_DUMP_ENC="$TEST_DIR/braga_backup_20261113_120000.dump.enc"
MOCK_SHA_FILE="${MOCK_DUMP_ENC}.sha256"

# Cria dump mock
echo "PGDUMP_HEADER_RESTORE_MOCK_DATA" > "$MOCK_DUMP_PLAIN"
head -c 2048 /dev/zero >> "$MOCK_DUMP_PLAIN"

# Criptografa o dump mock
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
    -pass pass:"$PASSPHRASE" \
    -in "$MOCK_DUMP_PLAIN" \
    -out "$MOCK_DUMP_ENC"

(cd "$TEST_DIR" && sha256sum "$(basename "$MOCK_DUMP_ENC")" > "$(basename "$MOCK_SHA_FILE")")

# Mock psql
cat << "EOF" > "$MOCK_BIN/psql"
#!/usr/bin/env bash
# Simula respostas para psql
CMD="$*"
if [[ "$CMD" =~ "SELECT current_database()" ]]; then
    echo "braga_restore_test"
    exit 0
fi
if [[ "$CMD" =~ "information_schema.tables" ]]; then
    echo "1"
    exit 0
fi
if [[ "$CMD" =~ "SELECT COUNT(*) FROM" ]]; then
    echo "42"
    exit 0
fi
if [[ "$CMD" =~ "SELECT version FROM schema_migrations" ]]; then
    echo "089_performance_targeted_indexes"
    exit 0
fi
echo "OK"
EOF
chmod +x "$MOCK_BIN/psql"

# Mock pg_restore
cat << "EOF" > "$MOCK_BIN/pg_restore"
#!/usr/bin/env bash
# Simula pg_restore bem sucedido
exit 0
EOF
chmod +x "$MOCK_BIN/pg_restore"

export PATH="$MOCK_BIN:$PATH"

echo "=== TESTE 1: Trava de Segurança - Ausência de TEST_RESTORE_DATABASE_URL ==="
set +e
TEST_RESTORE_DATABASE_URL="" BACKUP_ENCRYPTION_PASSPHRASE="$PASSPHRASE" "$SCRIPT_UNDER_TEST" --file "$MOCK_DUMP_ENC" >/dev/null 2>&1
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
    echo "  -> Trava acionada com sucesso (rejeitou banco indefinido)."
else
    echo "  -> [FALHA] Script permitiu executar sem TEST_RESTORE_DATABASE_URL!" >&2
    exit 1
fi

echo "=== TESTE 2: Trava de Segurança - Rejeição de URL idêntica à Produção ==="
set +e
TEST_RESTORE_DATABASE_URL="postgresql://prod:secret@db.internal:5432/braga_production" \
DATABASE_URL="postgresql://prod:secret@db.internal:5432/braga_production" \
BACKUP_ENCRYPTION_PASSPHRASE="$PASSPHRASE" "$SCRIPT_UNDER_TEST" --file "$MOCK_DUMP_ENC" >/dev/null 2>&1
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
    echo "  -> Trava acionada com sucesso (bloqueou colisão com URL de produção)."
else
    echo "  -> [FALHA] Script permitiu apontar para URL idêntica à produção!" >&2
    exit 1
fi

echo "=== TESTE 3: Trava de Segurança - Bloqueio heurístico de hostname produtivo RDS ==="
set +e
TEST_RESTORE_DATABASE_URL="postgresql://u:p@braga-saude-database.cde0e02i8btl.us-east-2.rds.amazonaws.com:5432/test" \
BACKUP_ENCRYPTION_PASSPHRASE="$PASSPHRASE" "$SCRIPT_UNDER_TEST" --file "$MOCK_DUMP_ENC" >/dev/null 2>&1
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
    echo "  -> Trava acionada com sucesso (bloqueou hostname produtivo do RDS)."
else
    echo "  -> [FALHA] Script permitiu apontar para endpoint RDS de produção!" >&2
    exit 1
fi

echo "=== TESTE 4: Detecção de Checksum SHA-256 Corrompido ==="
TAMPERED_DIR="$TEST_DIR/tampered"
mkdir -p "$TAMPERED_DIR"
cp "$MOCK_DUMP_ENC" "$TAMPERED_DIR/"
echo "0000000000000000000000000000000000000000000000000000000000000000  $(basename "$MOCK_DUMP_ENC")" > "$TAMPERED_DIR/$(basename "$MOCK_SHA_FILE")"

set +e
TEST_RESTORE_DATABASE_URL="postgresql://test:test@127.0.0.1:5432/braga_restore_test" \
BACKUP_ENCRYPTION_PASSPHRASE="$PASSPHRASE" "$SCRIPT_UNDER_TEST" --file "$TAMPERED_DIR/$(basename "$MOCK_DUMP_ENC")" >/dev/null 2>&1
STATUS=$?
set -e
if [ "$STATUS" -ne 0 ]; then
    echo "  -> Rejeitou artefato com hash corrompido com sucesso."
else
    echo "  -> [FALHA] Script aceitou checksum divergente!" >&2
    exit 1
fi

echo "=== TESTE 5: Fluxo Completo de Restauração em Banco de Teste Isolado ==="
export TEST_RESTORE_DATABASE_URL="postgresql://test:test@127.0.0.1:5432/braga_restore_test"
export SOURCE_DATABASE_URL="postgresql://read_only:pass@127.0.0.1:5432/braga_origin"
export BACKUP_ENCRYPTION_PASSPHRASE="$PASSPHRASE"

OUTPUT=$("$SCRIPT_UNDER_TEST" --file "$MOCK_DUMP_ENC")

if echo "$OUTPUT" | grep -q "TESTE DE RESTAURAÇÃO E AUDITORIA DE DR CONCLUÍDOS COM SUCESSO ABSOLUTO"; then
    echo "  -> Ensaio completo de restauração, verificação de tabelas e volumetria: SUCESSO!"
else
    echo "  -> [FALHA] Saída não indicou sucesso completo:" >&2
    echo "$OUTPUT" >&2
    exit 1
fi

echo ""
echo "================================================================================"
echo " TODAS AS TRAVAS E FLUXOS DE RESTAURAÇÃO DE DR FORAM APROVADOS!"
echo "================================================================================"
