#!/bin/bash
# ==============================================================================
# Runbook de Rollback Imediato: Rust Edge -> Python Standalone (SPEC-FASE-5 §6)
# Tempo de recuperação estimado: < 1 minuto (meta < 5 minutos).
# ==============================================================================
set -euo pipefail

echo "=== [ROLLBACK] Iniciando reversao imediata para Python Standalone ==="

# 1. Para o servico braga-edge (libera a porta 8080)
if systemctl is-active --quiet braga-edge.service; then
    echo "Parando braga-edge.service..."
    sudo systemctl stop braga-edge.service
    sudo systemctl disable braga-edge.service || true
fi

# 2. Restaura braga-gateway.service original (Gunicorn em 0.0.0.0:8080)
echo "Restaurando unit original braga-gateway.service em 0.0.0.0:8080..."
sudo cp -f /home/ec2-user/braga-gateway/infra/systemd/braga-gateway.service /etc/systemd/system/braga-gateway.service
sudo systemctl daemon-reload
sudo systemctl restart braga-gateway.service

# 3. Validacao de Liveness local
echo "Aguardando subida do gateway Python..."
sleep 3

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "=== [SUCESSO] Gateway Python restaurado e respondendo HTTP 200 em /health ==="
else
    echo "=== [ALERTA] Gateway respondeu status $HTTP_CODE — verificar logs com journalctl -u braga-gateway.service ==="
    exit 1
fi
