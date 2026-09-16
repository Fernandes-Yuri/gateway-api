#!/usr/bin/env bash
# Execute manually over SSH. Uses the existing database and braga-gateway.service.
set -Eeuo pipefail
umask 077
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_dir=/home/yurifjs/projetos/braga-gateway/app
python=/home/yurifjs/projetos/braga-gateway/venv/bin/python
service_account="${FIREBASE_SERVICE_ACCOUNT_PATH:-/home/yurifjs/projetos/braga-gateway/serviceAccountKey.json}"
modules=(server.py ws_orb.py profile_contract.py api_identity.py family_contract.py family_api.py email_service.py whatsapp_service.py)

echo '1/5 — Verificando arquivos, serviço e dependências existentes...'
test -d "$app_dir"
test -x "$python"
test -s "$service_account" || { echo 'Service account do Firebase ausente. Nada foi alterado.' >&2; exit 1; }
for module in "${modules[@]}"; do test -s "$package_dir/$module"; done
test -s "$package_dir/001_profile_and_family.sql"
sudo -v
sudo systemctl is-active --quiet braga-gateway
sudo docker exec bragasaude-postgres pg_isready -U bragasaude_user -d bragasaude
"$python" -c 'import fastapi, pydantic, asyncpg, firebase_admin, requests, httpx, edge_tts'
"$python" - "$service_account" <<'PY'
import sys
from firebase_admin import credentials
credentials.Certificate(sys.argv[1])
print('Credencial Firebase legível; conteúdo não exibido.')
PY
"$python" -m py_compile "${modules[@]/#/$package_dir/}"

echo '2/5 — Fazendo backup do banco e do código atual...'
backup_root=/home/yurifjs/backups/bragasaude
mkdir -p "$backup_root"
backup_dir="$(mktemp -d "$backup_root/alinhamento-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
mkdir "$backup_dir/app"
for module in "${modules[@]}"; do
    if [[ -f "$app_dir/$module" ]]; then cp -p "$app_dir/$module" "$backup_dir/app/"; fi
done
sudo docker exec bragasaude-postgres pg_dump -U bragasaude_user -d bragasaude -Fc > "$backup_dir/bragasaude.dump"
test -s "$backup_dir/bragasaude.dump"
sudo docker exec -i bragasaude-postgres pg_restore --list < "$backup_dir/bragasaude.dump" > "$backup_dir/conteudo-backup.txt"
echo "Backup: $backup_dir"

stopped=0
recover() {
    result=$?
    trap - ERR
    if [[ $stopped == 1 ]]; then
        echo 'Atualização falhou. Restaurando código anterior e reiniciando braga-gateway...' >&2
        cp -p "$backup_dir/app/"*.py "$app_dir/"
        sudo systemctl restart braga-gateway || true
    fi
    echo "O banco não foi apagado. Backup disponível em: $backup_dir" >&2
    exit "$result"
}
trap recover ERR

echo '3/5 — Aplicando a migração no banco existente...'
sudo systemctl stop braga-gateway
stopped=1
sudo docker exec -i bragasaude-postgres psql -X -v ON_ERROR_STOP=1 -U bragasaude_user -d bragasaude < "$package_dir/001_profile_and_family.sql"

echo '4/5 — Instalando os módulos da mesma API FastAPI...'
for module in "${modules[@]}"; do install -m 600 "$package_dir/$module" "$app_dir/$module"; done
"$python" -m py_compile "${modules[@]/#/$app_dir/}"
sudo systemctl restart braga-gateway

echo '5/5 — Verificando serviço, contrato da API e banco...'
ready=0
for attempt in {1..20}; do
    if curl --fail --silent --max-time 3 http://127.0.0.1:8080/health > "$backup_dir/health.json"; then
        if "$python" - "$backup_dir/health.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
assert state.get('profile_contract') == 1
assert state.get('database') == 'postgresql_16_alpine'
PY
        then ready=1; break; fi
    fi
    sleep 2
done
[[ $ready == 1 ]]
[[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:8080/api/profile/verification)" == 401 ]]
sudo systemctl is-active --quiet braga-gateway
sudo docker exec bragasaude-postgres psql -X -v ON_ERROR_STOP=1 -U bragasaude_user -d bragasaude -c "SELECT version, applied_at FROM schema_migrations WHERE version='001_profile_and_family';"
trap - ERR
echo "Atualização concluída. Backup preservado: $backup_dir"
echo 'Agora instale o APK correspondente e siga o roteiro de teste no aparelho.'
