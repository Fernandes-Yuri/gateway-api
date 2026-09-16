# =============================================================
# Deploy: server.py + caregiver_alerts.py + migração SQL
# Executar no computador que tem acesso à rede local 192.168.20.69
# =============================================================

$SERVER = "yurifjs@192.168.20.69"
$REMOTE_DIR = "/home/yurifjs/projetos/braga-gateway/app"
$REMOTE_MIGRATIONS = "/home/yurifjs/projetos/braga-gateway/migrations"
$PLINK = "$env:TEMP\plink.exe"
$PSCP = "$env:TEMP\pscp.exe"
$HOSTKEY = "SHA256:BYcWKDVMvk54CrFPSUHDsVVPN75hMbrRZ9kzrg85kkM"
$PASSWORD = "290220"
$LOCAL_DIR = "C:\bragasaude\scripts\server"
$LOCAL_MIGRATIONS = "C:\bragasaude\scripts\docker\migrations"

Write-Host "=== BragaSaude Server Deploy ===" -ForegroundColor Cyan
Write-Host ""

# 1. Backup do server.py atual no servidor
Write-Host "[1/5] Fazendo backup do server.py atual..." -ForegroundColor Yellow
& $PLINK -batch -hostkey $HOSTKEY -pw $PASSWORD $SERVER "cp $REMOTE_DIR/server.py $REMOTE_DIR/server.py.bak.$(date +%Y%m%d_%H%M%S) 2>/dev/null; echo 'Backup OK'"
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO no backup" -ForegroundColor Red; exit 1 }

# 2. Copiar server.py
Write-Host "[2/5] Copiando server.py..." -ForegroundColor Yellow
& $PSCP -batch -hostkey $HOSTKEY -pw $PASSWORD "$LOCAL_DIR\server.py" "${SERVER}:${REMOTE_DIR}/server.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO ao copiar server.py" -ForegroundColor Red; exit 1 }

# 3. Copiar caregiver_alerts.py
Write-Host "[3/5] Copiando caregiver_alerts.py..." -ForegroundColor Yellow
& $PSCP -batch -hostkey $HOSTKEY -pw $PASSWORD "$LOCAL_DIR\caregiver_alerts.py" "${SERVER}:${REMOTE_DIR}/caregiver_alerts.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO ao copiar caregiver_alerts.py" -ForegroundColor Red; exit 1 }

# 4. Criar diretorio de migracoes e copiar SQL
Write-Host "[4/5] Copiando migração SQL..." -ForegroundColor Yellow
& $PLINK -batch -hostkey $HOSTKEY -pw $PASSWORD $SERVER "mkdir -p $REMOTE_MIGRATIONS"
& $PSCP -batch -hostkey $HOSTKEY -pw $PASSWORD "$LOCAL_MIGRATIONS\002_medication_schedules.sql" "${SERVER}:${REMOTE_MIGRATIONS}/002_medication_schedules.sql"
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO ao copiar migração" -ForegroundColor Red; exit 1 }

# 5. Reiniciar servico
Write-Host "[5/5] Reiniciando servico qwen..." -ForegroundColor Yellow
& $PLINK -batch -hostkey $HOSTKEY -pw $PASSWORD $SERVER "sudo systemctl restart qwen && echo 'Servico reiniciado' || echo 'ERRO ao reiniciar'"
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO ao reiniciar servico" -ForegroundColor Red; exit 1 }

# Verificar status
Write-Host ""
Write-Host "=== Verificando status ===" -ForegroundColor Cyan
Start-Sleep -Seconds 2
& $PLINK -batch -hostkey $HOSTKEY -pw $PASSWORD $SERVER "sudo systemctl status qwen --no-pager -l | head -15"

Write-Host ""
Write-Host "=== Deploy concluido ===" -ForegroundColor Green
Write-Host ""
Write-Host "PROXIMO PASSO - Aplicar migracao SQL no banco:" -ForegroundColor Yellow
Write-Host "  docker exec -it bragasaude-postgres psql -U bragasaude_user -d bragasaude -f /home/yurifjs/projetos/braga-gateway/migrations/002_medication_schedules.sql"
Write-Host ""
Write-Host "Para verificar se o endpoint funciona:" -ForegroundColor Yellow
Write-Host "  curl -X POST http://localhost:8080/api/family/health-alert -H 'Content-Type: application/json' -d '{""patientId"":""test"",""caregiverId"":""test"",""message"":""teste""}'"
