# Procedimento de Recuperação de Desastres (DR) e Validação de Snapshots RDS

O banco de dados relacional do Braga Saúde (PostgreSQL) opera sob o Amazon RDS na região `us-east-2`, com armazenamento em disco criptografado em repouso via AWS KMS (`alias/aws/rds`). Este documento estabelece o protocolo de teste de restauração periódica e recuperação de desastres (DR) sem downtime da instância produtiva ativa.

---

## 1. Diretrizes de Segurança e Não-Interrupção

1. **Princípio do Isolamento:** Nunca execute um procedimento de restauração de teste ou ensaio operacional sobre o identificador da instância produtiva mestre (`braga-saude-database`).
2. **Criptografia Preservada:** Toda restauração de snapshot ou PITR (Point-in-Time Recovery) deve manter a criptografia com a chave KMS original.
3. **Contenção de Custos:** Instâncias provisionadas para ensaio de recuperação devem ser sumariamente destruídas após a emissão do parecer de integridade, impedindo faturamento ocioso no RDS.
4. **Proteção de Dados Sensíveis:** O ensaio valida estrutura de esquema, integridade referencial e leitura de contagens sem exportar dados clínicos de pacientes para arquivos locais ou ambientes externos.

---

## 2. Comandos Operacionais (AWS CLI)

### 2.1 Identificar o Snapshot Automático mais Recente

```bash
SNAPSHOT_ID=$(aws rds describe-db-snapshots \
    --db-instance-identifier "braga-saude-database" \
    --snapshot-type automated \
    --query "reverse(sort_by(DBSnapshots, &SnapshotCreateTime))[0].DBSnapshotIdentifier" \
    --output text)

echo "Snapshot identificado para ensaio: $SNAPSHOT_ID"
```

### 2.2 Restaurar Snapshot para Instância Temporária de Validação

Provisiona a réplica de teste sob o identificador `braga-db-restore-audit` dentro da mesma VPC e Subnet Group:

```bash
aws rds restore-db-instance-from-db-snapshot \
    --db-instance-identifier "braga-db-restore-audit" \
    --db-snapshot-identifier "$SNAPSHOT_ID" \
    --db-instance-class "db.t4g.micro" \
    --no-publicly-accessible \
    --kms-key-id "alias/aws/rds" \
    --tags Key=Environment,Value=Audit Key=Purpose,Value=DR-Validation
```

Aguardar a conclusão do provisionamento e ativação:

```bash
echo "Aguardando instancia temporaria ficar disponivel..."
aws rds wait db-instance-available \
    --db-instance-identifier "braga-db-restore-audit"
```

### 2.3 Obter Endpoint e Validar Integridade

Recuperar o endpoint privado da instância restaurada:

```bash
RESTORE_ENDPOINT=$(aws rds describe-db-instances \
    --db-instance-identifier "braga-db-restore-audit" \
    --query "DBInstances[0].Endpoint.Address" \
    --output text)

echo "Endpoint de validacao: $RESTORE_ENDPOINT"
```

Validação de integridade sem expor registros sensíveis (executado a partir da EC2 interna):

```bash
PGPASSWORD="$DB_PASSWORD" psql -h "$RESTORE_ENDPOINT" -U postgres -d braga_production -c "
    SELECT count(*) AS total_migrations FROM schema_migrations;
    SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;
"
```

---

## 3. Procedimento de Restauração Pontual (PITR)

Em caso de corrupção lógica de dados ou falha humana durante manutenção, o RDS permite restaurar o banco para qualquer segundo dentro da janela de retenção (RPO de ~5 minutos):

```bash
# Define o ponto de restauracao (exemplo: 10 minutos antes do incidente)
RESTORE_TIME="2026-11-11T14:30:00Z"

aws rds restore-db-instance-to-point-in-time \
    --source-db-instance-identifier "braga-saude-database" \
    --target-db-instance-identifier "braga-db-pitr-recovery" \
    --restore-time "$RESTORE_TIME" \
    --db-instance-class "db.t4g.micro" \
    --no-publicly-accessible

aws rds wait db-instance-available \
    --db-instance-identifier "braga-db-pitr-recovery"
```

---

## 4. Cutover e Chaveamento em Cenário Real de Desastre

Se a instância primária for irrecuperável e o banco restaurado for promovido para produção:

1. Atualizar o parâmetro de segredo no AWS SSM Parameter Store:
   ```bash
   NEW_DB_URL="postgresql://user:pass@$RESTORE_ENDPOINT:5432/braga_production"
   aws ssm put-parameter \
       --name "/braga-saude/production/DATABASE_URL" \
       --value "$NEW_DB_URL" \
       --type "SecureString" \
       --overwrite
   ```
2. Executar deploy limpo na EC2 para regenerar o `/run/braga-saude/gateway.env` e reiniciar os serviços `braga-edge`, `braga-chat` e `braga-gateway`:
   ```bash
   sudo systemctl restart braga-edge braga-chat braga-gateway
   ```

---

## 5. Limpeza de Instâncias Temporárias de Ensaio

Após a conclusão dos testes de auditoria, eliminar a instância temporária imediatamente sem gerar snapshot final para evitar custos:

```bash
aws rds delete-db-instance \
    --db-instance-identifier "braga-db-restore-audit" \
    --skip-final-snapshot \
    --delete-automated-backups

echo "Instancia temporaria de DR expurgada com sucesso."
```
