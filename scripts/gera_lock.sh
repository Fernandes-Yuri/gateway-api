#!/usr/bin/env bash
# Regenera os locks de dependencias com hashes (DT-015).
#
# inputs : requirements.txt (runtime) e requirements-dev.txt (dev/CI)
# outputs: requirements.lock e requirements-dev.lock (GERADOS — nao editar)
#
# Alvo fixado: CPython 3.12 x86_64-linux, o mesmo do CI (setup-python 3.12,
# ubuntu-latest) e do venv da EC2 (/usr/bin/python3.12). mudar o alvo =
# regenerar aqui, nunca a mao.
#
# Atualizar versoes (primeira atualizacao por PR do DT-015):
#   gera_lock.sh --upgrade google-auth      # rola so os pacotes nomeados
#   gera_lock.sh --upgrade-all              # rola tudo ao mais novo resolvivel
# Sem flag, a regeneracao e idempotente: mantem os pins existentes.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_ALVO="3.12"
PLATAFORMA="x86_64-unknown-linux-gnu"
UPGRADE_PKGS=()
UPGRADE_ALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --upgrade)
            [ $# -ge 2 ] || { echo "erro: --upgrade precisa do nome do pacote" >&2; exit 1; }
            UPGRADE_PKGS+=("$2"); shift 2 ;;
        --upgrade-all)
            UPGRADE_ALL=1; shift ;;
        *)
            echo "uso: $0 [--upgrade PACOTE ...] [--upgrade-all]" >&2; exit 1 ;;
    esac
done

if ! command -v uv >/dev/null 2>&1; then
    echo "erro: 'uv' nao encontrado (instale: pip install uv)" >&2
    exit 1
fi

# Flags de upgrade para o uv pip compile: --upgrade-package e seletivo
# (so sobe os nomeados, mantendo o resto idempotente); --upgrade sobe tudo.
UPGRADE_FLAGS=()
if [ "$UPGRADE_ALL" -eq 1 ]; then
    UPGRADE_FLAGS=(--upgrade)
else
    for pkg in ${UPGRADE_PKGS[@]+"${UPGRADE_PKGS[@]}"}; do
        UPGRADE_FLAGS+=(--upgrade-package "$pkg")
    done
fi

gera() {
    local entrada="$1" saida="$2"
    echo "==> $entrada -> $saida (python $PYTHON_ALVO, $PLATAFORMA)"
    uv pip compile "$entrada" \
        --python-version "$PYTHON_ALVO" \
        --python-platform "$PLATAFORMA" \
        --generate-hashes \
        --quiet \
        ${UPGRADE_FLAGS[@]+"${UPGRADE_FLAGS[@]}"} \
        --output-file "$saida"
    linhas=$(grep -cE '^[a-zA-Z0-9]' "$saida" || true)
    pacotes=$(grep -cE '^[a-zA-Z0-9].*==' "$saida" || true)
    hashes=$(grep -c -- '--hash=sha256:' "$saida" || true)
    echo "    $pacotes pacotes fixados, $hashes hashes, $linhas linhas de requisito"
}

gera requirements.txt requirements.lock
gera requirements-dev.txt requirements-dev.lock

# Guarda de coerencia: o lock de dev tem de conter todo o runtime.
faltam=$(comm -23 \
    <(grep -oE '^[a-zA-Z0-9_.-]+' requirements.lock | sort -u) \
    <(grep -oE '^[a-zA-Z0-9_.-]+' requirements-dev.lock | sort -u) | tr '\n' ' ')
if [ -n "$faltam" ]; then
    echo "erro: requirements-dev.lock nao contem as dependencias de runtime: $faltam" >&2
    exit 1
fi

echo "ok: locks gerados e coerentes entre si"
