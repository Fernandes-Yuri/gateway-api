# Procedimento Operacional: Backup Soberano Diário & Multi-Cloud (PostgreSQL RDS -> Cloudflare R2)

Este documento estabelece a arquitetura técnica, isolamento de credenciais, automação via Systemd e o **Protocolo de Restauração Agnóstica (DR Runbook)** para o backup lógico do PostgreSQL do Braga Saúde em nuvem externa (Cloudflare R2).

---

## 1. Princípios e Estratégia de Saída (*Cloud-Exit*)

1. **Regra 3-2-1:**
   - **3 cópias:** Banco de produção RDS + snapshots automáticos AWS + dump lógico cifrado.
   - **2 mídias:** Volume EBS RDS + bucket de objetos Cloudflare R2.
   - **1 cópia offsite (fora da AWS):** Cloudflare R2 (região ENAM).
2. **Custo Zero:** R2 possui tráfego de egress 100% gratuito e retenção de 7 dias (< 400 MB), bem abaixo da cota gratuita vitalícia de 10 GB.
3. **Criptografia Soberana:** Criptografia AES-256-CBC via OpenSSL com derivação de chave PBKDF2 (100.000 iterações), sem qualquer dependência do AWS KMS.
4. **Proteção LGPD:** O dump binário gerado pelo `pg_dump -Fc` é encadeado por stream direto para a cifragem do OpenSSL, sem gravação de dados desprotegidos no disco da EC2.

---

## 2. Componentes e Estrutura de Arquivos

- **Script principal de backup:** `scripts/maintenance/backup_soberano_r2.sh`
- **Script de ensaio e validação de restauração:** `scripts/maintenance/test_restore_verification.sh`
- **Unit Systemd Service:** `infra/systemd/braga-backup.service`
- **Unit Systemd Timer:** `infra/systemd/braga-backup.timer` (agendado diariamente para 03:30 BRT / 06:30 UTC)
- **Arquivo de segredos isolado na EC2:** `/etc/braga-saude/r2-backup.env` (`chmod 600`, proprietário `ec2-user:ec2-user`)

### Conteúdo de `/etc/braga-saude/r2-backup.env`

```ini
CF_R2_ACCESS_KEY_ID="<TOKEN_ACCESS_KEY_ID>"
CF_R2_SECRET_ACCESS_KEY="<TOKEN_SECRET_ACCESS_KEY>"
CF_R2_ENDPOINT_URL="https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
CF_R2_BUCKET_NAME="braga-saude"
BACKUP_ENCRYPTION_PASSPHRASE="<CHAVE_MESTRA_AES256>"
BACKUP_DAILY_RETENTION_DAYS=7
BACKUP_WEEKLY_RETENTION_DAYS=28
BACKUP_WEEKLY_DAY=7
BACKUP_PREFIX="backups"
```

---

## 3. Protocolo de Restauração Agnóstica (Runbook de DR em 3 Minutos)

Este procedimento pode ser executado em qualquer máquina Linux, servidor bare-metal, VPS de outro provedor ou container Docker local sem depender de serviços da AWS.

### Pré-requisitos
- `aws-cli` (ou `rclone`, `curl`, `minio-client`)
- `openssl` (qualquer versão moderna com suporte a PBKDF2)
- `postgresql-client` (`pg_restore` v16+)

### Passo 1: Download do Backup e Checksum a partir do Cloudflare R2

```bash
BACKUP_NAME="braga_backup_YYYYMMDD_HHMMSS" # Substitua pelo timestamp desejado

# Download do dump cifrado
aws s3 cp "s3://braga-saude/backups/${BACKUP_NAME}.dump.enc" ./ \
    --endpoint-url "https://5d4480fa9abc690d2bb528b54c56d99b.r2.cloudflarestorage.com" \
    --region auto

# Download do hash de verificação
aws s3 cp "s3://braga-saude/backups/${BACKUP_NAME}.dump.enc.sha256" ./ \
    --endpoint-url "https://5d4480fa9abc690d2bb528b54c56d99b.r2.cloudflarestorage.com" \
    --region auto
```

### Passo 2: Verificação Criptográfica de Integridade

```bash
sha256sum -c "${BACKUP_NAME}.dump.enc.sha256"
# Saída esperada: braga_backup_YYYYMMDD_HHMMSS.dump.enc: SUCESSO
```

### Passo 3: Descriptografia com a Chave Soberana

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass pass:"$BACKUP_ENCRYPTION_PASSPHRASE" \
    -in "${BACKUP_NAME}.dump.enc" \
    -out braga_restaurado.dump
```

### Passo 4: Inspeção e Restauração no PostgreSQL

Para inspecionar o catálogo do dump sem aplicar no banco:
```bash
pg_restore --list braga_restaurado.dump | head -n 30
```

Para restaurar em uma nova instância ou container PostgreSQL:
```bash
pg_restore -h <HOST_NOVO> -U <USUARIO_NOVO> -d <BANCO_NOVO> -v --clean --if-exists braga_restaurado.dump
```

---

## 4. Auditoria Automatizada e Ensaio Periódico de DR

Para garantir que os backups gerados são 100% recuperáveis e que nenhuma regressão de schema ocorra sem ser detectada, utilize o utilitário de validação automatizada:

```bash
# Executado apontando estritamente para uma base de testes separada:
TEST_RESTORE_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/braga_audit_test" \
BACKUP_ENCRYPTION_PASSPHRASE="sua_chave_aqui" \
SOURCE_DATABASE_URL="postgresql://readonly:pass@.../braga_production" \
./scripts/maintenance/test_restore_verification.sh
```

O script executa:
1. Verificação da trava de segurança (recusa executar se o alvo for a produção).
2. Download do último backup do Cloudflare R2 e verificação do SHA-256.
3. Descriptografia e restauração no banco de teste.
4. Auditoria de presença das tabelas do schema clínico (`profiles`, `vital_signs`, `care_plans`, `medications`, etc.).
5. Comparação volumétrica de contagem de linhas entre a origem e o banco restaurado.
6. Emissão de parecer de integridade e descarte imediato dos artefatos descriptografados em disco.
