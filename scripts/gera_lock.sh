#!/usr/bin/env bash
# Regenera os locks de dependencias com hashes (DT-015).
#
# inputs : requirements.txt (runtime) e requirements-dev.txt (dev/CI)
# outputs: requirements.lock e requirements-dev.lock (GERADOS — nao editar)
#
# Alvo fixado: CPython 3.11 x86_64-linux, o mesmo do CI (setup-python 3.11,
# ubuntu-latest) e da EC2. mudar o alvo = regenerar aqui, nunca a mao.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_ALVO="3.11"
PLATAFORMA="x86_64-unknown-linux-gnu"

if ! command -v uv >/dev/null 2>&1; then
    echo "erro: 'uv' nao encontrado (instale: pip install uv)" >&2
    exit 1
fi

gera() {
    local entrada="$1" saida="$2"
    echo "==> $entrada -> $saida (python $PYTHON_ALVO, $PLATAFORMA)"
    uv pip compile "$entrada" \
        --python-version "$PYTHON_ALVO" \
        --python-platform "$PLATAFORMA" \
        --generate-hashes \
        --quiet \
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
