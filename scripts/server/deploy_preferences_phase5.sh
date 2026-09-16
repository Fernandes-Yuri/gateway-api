#!/usr/bin/env bash
set -euo pipefail
app=/home/yurifjs/projetos/braga-gateway/app
stage=/home/yurifjs/projetos/braga-gateway/preference-stage-20260915
python=/home/yurifjs/projetos/braga-gateway/venv/bin/python3
cd "$app"
printf '%s\n' \
 'f48b2a2d5d400d7fcce93b4bf2d60dde5f73005874b622284ec28d6b979913ed  server.py' \
 'ccfdb4e51a1726313851c5f7996e6192e5047794833f6b637b72db6acc1b81b6  ws_orb.py' \
 '6eb9e4d55d59196bce032f199700b350c197d01e0aa7f61f955590da1acf107d  orb_llm.py' | sha256sum -c -
"$python" -m py_compile "$stage/server.py" "$stage/ws_orb.py" "$stage/orb_llm.py" "$stage/assistant_preferences.py"
backup=$(mktemp -d /home/yurifjs/backups/bragasaude/pre_preferences_20260915_XXXXXX)
files=(server.py ws_orb.py orb_llm.py assistant_preferences.py test_assistant_preferences.py test_ws_orb.py)
for file in "${files[@]}"; do
  if [ -f "$app/$file" ]; then cp -p "$app/$file" "$backup/$file"; fi
done
printf 'Backup: %s\n' "$backup"
rollback() {
  echo 'Falha na ativação. Restaurando os módulos anteriores.'
  for file in "${files[@]}"; do
    if [ -f "$backup/$file" ]; then cp -p "$backup/$file" "$app/$file"; fi
  done
  sudo -n systemctl restart braga-gateway
}
trap rollback ERR
for file in "${files[@]}"; do cp "$stage/$file" "$app/$file"; done
sudo -n systemctl restart braga-gateway
"$python" - <<'PY'
import json, time, urllib.request, urllib.error
for attempt in range(10):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3) as response:
            data = json.load(response)
        assert data['status'] == 'online'
        break
    except Exception:
        if attempt == 9: raise
        time.sleep(2)
try:
    urllib.request.urlopen('http://127.0.0.1:8080/api/assistant/preferences', timeout=3)
    raise AssertionError('Endpoint should require authentication')
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
print('Health online; endpoint de preferências exige autenticação (401).')
PY
systemctl is-active braga-gateway
trap - ERR
