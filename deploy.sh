#!/bin/bash
set -e

APP_DIR="/home/ec2-user/braga-gateway"
cd "$APP_DIR"

echo "=== [1/4] Sincronizando scripts do servidor ==="
if [ -d "scripts/server" ] && [ -d "app" ]; then
    rsync -av --delete --exclude '__pycache__' --exclude '*.pyc' scripts/server/ app/
fi

# Limpeza de binarios/modelos legados do Piper TTS da EC2 se existirem
if [ -d "/home/ec2-user/piper" ]; then
    echo "Expurgando arquivos legados do Piper TTS da EC2..."
    rm -rf /home/ec2-user/piper
fi

echo "=== [2/4] Verificando dependencias Python ==="
# DT-015: o lock com hashes e a unica fonte de versoes em producao e e
# gerado para CPython 3.12 (scripts/gera_lock.sh). O venv da EC2 tem de ser
# dessa mesma versao: se nao for, recria com python3.12 antes de instalar.
# Regra: o venv antigo so e removido DEPOIS do novo criado, com o lock
# instalado e importando o que o runtime precisa (servico segue no ar).
VENV_ALVO="3.12"
if [ -x venv/bin/python ]; then
    VENV_VERSAO="$(venv/bin/python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
else
    VENV_VERSAO=""
fi
if [ "$VENV_VERSAO" != "$VENV_ALVO" ]; then
    echo "venv na versao '${VENV_VERSAO:-nenhum}'; recriando em $VENV_ALVO..."
    if ! command -v python3.12 >/dev/null 2>&1; then
        echo "ERRO: python3.12 nao encontrado — abortando sem tocar no venv atual." >&2
        exit 1
    fi
    python3.12 -m venv venv.novo
    venv.novo/bin/python -m pip install --upgrade pip --quiet
    if [ -f "requirements.lock" ]; then
        venv.novo/bin/python -m pip install --require-hashes -r requirements.lock --quiet
    elif [ -f "app/requirements.lock" ]; then
        venv.novo/bin/python -m pip install --require-hashes -r app/requirements.lock --quiet
    fi
    venv.novo/bin/python -c "import asyncpg, boto3, dotenv" || {
        echo "ERRO: venv novo nao ficou funcional — mantendo o venv antigo." >&2
        rm -rf venv.novo
        exit 1
    }
    rm -rf venv
    mv venv.novo venv
fi
source venv/bin/activate
# --require-hashes faz o pip recusar qualquer pacote cujo SHA256 nao bate
# com o registado no lock (verificado: adulterar um hash aborta o install).
if [ -f "requirements.lock" ]; then
    pip install --require-hashes -r requirements.lock --quiet
elif [ -f "app/requirements.lock" ]; then
    pip install --require-hashes -r app/requirements.lock --quiet
else
    # Transicao apenas: EC2 ainda sem o lock sincronizado.
    echo "AVISO: requirements.lock ausente — instalando sem lock (nao reproduzivel)."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt --quiet
    elif [ -f "app/requirements.txt" ]; then
        pip install -r app/requirements.txt --quiet
    fi
fi

# SPEC-11: Carregamento e provisionamento hibrido de segredos via AWS SSM + KMS.
# Sincroniza e injeta variaveis sensiveis em memoria (/run tmpfs) para o
# migrate.py e para o runtime do systemd, sem gravar segredos em disco.
export USE_AWS_SECRETS="${USE_AWS_SECRETS:-1}"
export AWS_SSM_PREFIX="${AWS_SSM_PREFIX:-/braga-saude/production}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
RUNTIME_ENV_DIR="/run/braga-saude"
RUNTIME_ENV_FILE="/run/braga-saude/gateway.env"
sudo install -d -m 0700 -o ec2-user -g ec2-user "$RUNTIME_ENV_DIR"
for LEGACY_ENV in "$APP_DIR/.env" "$APP_DIR/app/.env"; do
    if [ -f "$LEGACY_ENV" ]; then
        PYTHONPATH=scripts/server python3 -m secrets_loader --sync-from-dotenv "$LEGACY_ENV" || true
    fi
done
PYTHONPATH=scripts/server python3 -m secrets_loader \
    --ensure-pepper \
    --write-runtime-env "$RUNTIME_ENV_FILE"
if [ -f "$RUNTIME_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$RUNTIME_ENV_FILE"
    set +a
fi

echo "=== [3/4] Executando Migrations do Banco de Dados (RDS) ==="
if [ -f "migrate.py" ]; then
    # Não permitir gravações do atendimento antigo durante o expurgo.
    if [ ! -f "$APP_DIR/.retired_health_whatsapp_v1" ]; then
        sudo systemctl stop braga-gateway.service
        python3 scripts/purge_retired_channel_files.py
    fi
    python3 migrate.py
    python3 scripts/purge_legacy_exam_files.py
fi

# Não persistir dumps com memória de atendimentos. A política global também
# desabilita processamento pelo systemd-coredump quando o core_pattern usa pipe.
sudo install -d -m 0755 /etc/systemd/system/braga-gateway.service.d /etc/systemd/coredump.conf.d
sudo install -m 0644 infra/systemd/braga-gateway.service.d/10-private-memory.conf /etc/systemd/system/braga-gateway.service.d/10-private-memory.conf
sudo install -m 0644 infra/systemd/coredump.conf.d/10-braga-privacy.conf /etc/systemd/coredump.conf.d/10-braga-privacy.conf
# SPEC-04: a unidade agora vive no repositorio (gunicorn com 2 workers Uvicorn).
sudo install -m 0644 infra/systemd/braga-gateway.service /etc/systemd/system/braga-gateway.service
sudo systemctl daemon-reload

echo "=== [4/4] Reiniciando Braga Gateway ==="
sudo systemctl restart braga-gateway.service
sleep 2

# Verificacao de integridade
HEALTH=$(curl -s http://127.0.0.1:8080/health || echo "{}")
if echo "$HEALTH" | grep -q '"status":"online"'; then
    echo "================================================="
    echo "DEPLOY CONCLUIDO COM SUCESSO: Gateway esta ONLINE!"
    echo "================================================="
else
    echo "[ERRO] Health check falhou apos reinicio:"
    echo "$HEALTH"
    sudo systemctl status braga-gateway.service --no-pager
    exit 1
fi