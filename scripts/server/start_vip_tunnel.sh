#!/usr/bin/env bash
# Temporary systemd-managed test tunnel; braga-gateway stays on port 8080.
set -Eeuo pipefail
app_dir=/home/yurifjs/projetos/braga-gateway/app
python=/home/yurifjs/projetos/braga-gateway/venv/bin/python
test -s "$app_dir/vip_public_proxy.py"
test -x /usr/local/bin/cloudflared
sudo -v
if ! systemctl is-active --quiet braga-vip-public; then
    sudo systemd-run --unit=braga-vip-public --collect --uid=yurifjs --working-directory="$app_dir" \
      "$python" -m uvicorn vip_public_proxy:app --host 127.0.0.1 --port 8081 --no-proxy-headers --no-access-log
fi
ready=0
for attempt in {1..10}; do
    if curl --fail --silent --max-time 3 http://127.0.0.1:8081/api/public/vip-signups/health; then ready=1; break; fi
    sleep 1
done
[[ $ready == 1 ]]
[[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 3 http://127.0.0.1:8081/api/profile/verification)" == 404 ]]
if ! systemctl is-active --quiet braga-vip-tunnel; then
    sudo systemd-run --unit=braga-vip-tunnel --collect --uid=yurifjs \
      /usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8081
fi
for attempt in {1..15}; do
    tunnel_url=$(sudo journalctl -u braga-vip-tunnel --since '5 minutes ago' --no-pager \
      | grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' \
      | grep -v '^https://api.trycloudflare.com$' | tail -n 1 || true)
    if [[ -n "$tunnel_url" ]]; then
        printf '\nURL TEMPORARIA: %s\n' "$tunnel_url"
        echo 'Envie somente essa URL para configurar o site. Ela pode mudar quando o túnel reiniciar.'
        exit 0
    fi
    sleep 2
done
echo 'URL não encontrada. Verifique: sudo journalctl -u braga-vip-tunnel -n 30 --no-pager' >&2
exit 1
