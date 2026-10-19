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
    venv.novo/bin/python -c "import asyncpg, dotenv" || {
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