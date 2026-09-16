#!/usr/bin/env bash
# Permanent Cloudflare tunnel for WhatsApp webhook (port 8080).
# URL stays the same as long as the service keeps running.
set -Eeuo pipefail

app_dir=/home/yurifjs/projetos/braga-gateway/app
url_file="$app_dir/.whatsapp_tunnel_url"
service_name="braga-whatsapp-tunnel"

echo "=== Cloudflare Tunnel Permanente — WhatsApp Webhook ==="

# 1. Stop temporary VIP tunnel if running
if systemctl is-active --quiet braga-vip-tunnel 2>/dev/null; then
    echo "Parando túnel temporário VIP..."
    sudo systemctl stop braga-vip-tunnel || true
    sudo systemctl disable braga-vip-tunnel 2>/dev/null || true
fi
if systemctl is-active --quiet braga-vip-public 2>/dev/null; then
    echo "Parando proxy VIP temporário..."
    sudo systemctl stop braga-vip-public || true
    sudo systemctl disable braga-vip-public 2>/dev/null || true
fi

# 2. Verify cloudflared is available
test -x /usr/local/bin/cloudflared || { echo 'cloudflared não encontrado em /usr/local/bin/cloudflared' >&2; exit 1; }

# 3. Verify main server is running
if ! curl --fail --silent --max-time 3 http://127.0.0.1:8080/health > /dev/null 2>&1; then
    echo "AVISO: Servidor principal (porta 8080) não está respondendo."
    echo "O túnel funcionará, mas o webhook só responderá quando o servidor estiver online."
fi

# 4. Create systemd service for permanent tunnel
sudo tee /etc/systemd/system/${service_name}.service > /dev/null <<EOF
[Unit]
Description=Cloudflare Tunnel — WhatsApp Webhook (port 8080)
After=network.target

[Service]
Type=simple
User=yurifjs
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080
Restart=always
RestartSec=5
Environment=HOME=/home/yurifjs

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${service_name}
sudo systemctl restart ${service_name}

echo "Serviço ${service_name} iniciado. Aguardando URL..."

# 5. Capture tunnel URL (wait up to 30 seconds)
tunnel_url=""
for attempt in $(seq 1 15); do
    tunnel_url=$(sudo journalctl -u ${service_name} --since '2 minutes ago' --no-pager \
      | grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' \
      | grep -v '^https://api.trycloudflare.com$' | tail -n 1 || true)
    if [[ -n "$tunnel_url" ]]; then
        break
    fi
    sleep 2
done

if [[ -z "$tunnel_url" ]]; then
    echo "ERRO: URL do túnel não encontrada. Verifique:" >&2
    echo "  sudo journalctl -u ${service_name} -n 30 --no-pager" >&2
    exit 1
fi

# 6. Save URL permanently
echo "$tunnel_url" > "$url_file"
chmod 600 "$url_file"

echo ""
echo "============================================"
echo "URL PERMANENTE DO TÚNEL:"
echo "  $tunnel_url"
echo "============================================"
echo ""
echo "URL salva em: $url_file"
echo ""
echo "Para configurar o webhook do WhatsApp no Meta:"
echo "  Callback URL: ${tunnel_url}/api/whatsapp/webhook"
echo "  Verify Token: (use o valor de WHATSAPP_VERIFY_TOKEN)"
echo ""
echo "Para verificar a URL depois:"
echo "  cat $url_file"
echo ""
echo "Para parar o túnel:"
echo "  sudo systemctl stop ${service_name}"
echo ""
echo "Para iniciar novamente:"
echo "  sudo systemctl start ${service_name}"
