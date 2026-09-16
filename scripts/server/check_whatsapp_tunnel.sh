#!/usr/bin/env bash
# Check WhatsApp tunnel status and test webhook endpoint.
set -Eeuo pipefail

app_dir=/home/yurifjs/projetos/braga-gateway/app
url_file="$app_dir/.whatsapp_tunnel_url"
service_name="braga-whatsapp-tunnel"

echo "=== Status do Túnel WhatsApp ==="

# Check service status
if systemctl is-active --quiet ${service_name} 2>/dev/null; then
    echo "Serviço: ATIVO"
else
    echo "Serviço: INATIVO"
    echo "Para iniciar: sudo systemctl start ${service_name}"
    exit 1
fi

# Get URL
if [[ ! -f "$url_file" ]]; then
    echo "URL não encontrada. Execute: start_whatsapp_tunnel.sh"
    exit 1
fi

tunnel_url=$(cat "$url_file")
echo "URL: $tunnel_url"

# Test tunnel connectivity
echo ""
echo "--- Teste de Conectividade ---"

# Test health endpoint
echo -n "Health (porta 8080): "
if curl --fail --silent --max-time 5 http://127.0.0.1:8080/health > /dev/null 2>&1; then
    echo "OK"
else
    echo "FALHOU"
fi

# Test webhook via tunnel
echo -n "Webhook via túnel: "
response=$(curl --silent --max-time 10 -o /dev/null -w "%{http_code}" \
  "${tunnel_url}/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=test&hub.challenge=test123" 2>&1)
if [[ "$response" == "403" ]]; then
    echo "OK (403 = token inválido, mas endpoint está respondendo)"
elif [[ "$response" == "200" ]]; then
    echo "OK (200 = verificação bem-sucedida)"
else
    echo "FALHOU (HTTP $response)"
fi

echo ""
echo "URL para configurar no Meta WhatsApp:"
echo "  Callback URL: ${tunnel_url}/api/whatsapp/webhook"
