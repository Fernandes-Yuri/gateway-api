#!/usr/bin/env bash
set -euo pipefail
app=/home/yurifjs/projetos/braga-gateway/app
stage=/home/yurifjs/projetos/braga-gateway/modular-stage-20260915
python=/home/yurifjs/projetos/braga-gateway/venv/bin/python3
cd "$app"
printf '%s\n' \
 'e10bfe5fab6fde832f7f0a2f8b9c5a055ae03fb6ecc172f3ae7246b027ab792f  server.py' \
 '80a9de8cb230f25acd5225c35c21f71540da945a32ede30b478a7d14e60448fa  orb_llm.py' | sha256sum -c -
"$python" -m py_compile "$stage/server.py" "$stage/orb_llm.py" "$stage/piper_service.py" "$stage/conversation_policy.py"
backup=$(mktemp -d /home/yurifjs/backups/bragasaude/pre_modular_20260915_XXXXXX)
files=(server.py orb_llm.py piper_service.py conversation_policy.py test_piper_service.py test_conversation_policy.py)
for file in "${files[@]}"; do
  if [ -f "$app/$file" ]; then cp -p "$app/$file" "$backup/$file"; fi
done
printf 'Backup: %s\n' "$backup"
rollback() {
  echo 'Restaurando módulos anteriores após falha de ativação.'
  for file in "${files[@]}"; do
    if [ -f "$backup/$file" ]; then cp -p "$backup/$file" "$app/$file"; fi
  done
  sudo -n systemctl restart braga-gateway
}
trap rollback ERR
for file in "${files[@]}"; do cp "$stage/$file" "$app/$file"; done
sudo -n systemctl restart braga-gateway
"$python" - <<'PY'
import json, time, urllib.request
for attempt in range(10):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3) as response:
            assert json.load(response)['status'] == 'online'
        break
    except Exception:
        if attempt == 9: raise
        time.sleep(2)
request = urllib.request.Request('http://127.0.0.1:8080/v1/audio/speech',
    data=json.dumps({'text': 'Este é um teste da organização do serviço de voz.', 'voice': 'faber'}).encode(),
    headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=45) as response:
    audio = response.read()
assert audio[:4] == b'RIFF' and audio[8:12] == b'WAVE' and len(audio) > 44
print('Health online e endpoint de voz retornando WAV válido.')
PY
systemctl is-active braga-gateway
trap - ERR
