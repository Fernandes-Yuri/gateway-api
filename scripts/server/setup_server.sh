#!/bin/bash
# =============================================================
# SCRIPT DE SETUP HOMELAB LENOVO G460 (DOCKER + POSTGRESQL + API)
# =============================================================
set -e

echo ">>> 1. Verificando Docker e Docker Compose..."
if ! command -v docker &> /dev/null; then
    echo "Instalando Docker..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg lsb-release
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo usermod -aG docker $USER
fi

echo ">>> 2. Criando diretório de dados e iniciando PostgreSQL via Docker..."
mkdir -p /home/yurifjs/projetos/bragasaude_docker
cp docker-compose.yml /home/yurifjs/projetos/bragasaude_docker/
cp init_schema.sql /home/yurifjs/projetos/bragasaude_docker/
cd /home/yurifjs/projetos/bragasaude_docker

sudo docker compose up -d

echo ">>> 3. Instalando dependência asyncpg no ambiente Python..."
cd /home/yurifjs/projetos/braga-gateway
if [ -d "venv" ]; then
    ./venv/bin/pip install asyncpg psycopg2-binary
else
    pip3 install asyncpg psycopg2-binary
fi

echo ">>> 4. Atualizando server.py..."
cp /tmp/server.py /home/yurifjs/projetos/braga-gateway/app/server.py

echo ">>> 5. Reiniciando serviço braga-gateway..."
sudo systemctl restart braga-gateway

echo ">>> SUCESSO! Banco PostgreSQL e API em execução."
