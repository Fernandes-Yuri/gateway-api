# Runbook — Deploy manual de binários Go (`braga-chat`) e Rust (`braga-edge`) na EC2

> **Contexto:** o `deploy.sh` disparado pelo workflow `deploy.yml` cobre **apenas o backend Python**
> (`braga-gateway.service`). Os binários nativos **Go** e **Rust** são publicados como artefatos de CI
> e precisam ser instalados manualmente na EC2 via **S3 + AWS SSM** (sem SSH exposto).
>
> **Quando usar:** sempre que `go-ci.yml` ou `rust-ci.yml` gerarem binários novos no `master`
> (ex.: mudanças em `go/`, `rust/`, ou roteamento da borda).
> **Ordem de segurança:** para mudanças de roteamento, publique **primeiro o Go** (se o destino da rota
> é o Go) e **depois o Rust**. O contrário pode encaminhar tráfego para um endpoint ainda inexistente.

---

## 1. Pré-requisitos

- `gh` autenticado no repositório `Fernandes-Yuri/gateway-api`.
- `aws` CLI com credenciais que permitam `s3:*` no bucket de deploy e `ssm:SendCommand`/`GetCommandInvocation`.
- Bucket de deploy: `braga-saude-deploy-397759110014` (prefixo `bin/`).
- Instância EC2: `i-04728dd8094ed0e78` (região `us-east-2`).
- Serviços: `braga-chat.service` (Go) e `braga-edge.service` (Rust) gerenciados pelo systemd.

---

## 2. Obter o artefato do CI (run verde do `master`)

```bash
# Listar runs recentes do master
gh run list --repo Fernandes-Yuri/gateway-api --branch master --limit 10

# Artefato do Go (nome "braga-chat-binary") do run do workflow go-ci.yml
RUN_GO=<run-id>
ART_ID=$(gh api repos/Fernandes-Yuri/gateway-api/actions/runs/$RUN_GO/artifacts \
  --jq '.artifacts[] | select(.name=="braga-chat-binary") | .id' | head -1)
curl -sL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  -o /tmp/braga-chat.zip \
  "https://api.github.com/repos/Fernandes-Yuri/gateway-api/actions/artifacts/$ART_ID/zip"
unzip -o /tmp/braga-chat.zip -d /tmp/braga-deploy

# Artefato do Rust (nome "rust-binaries"/arquivos em rust/target/release) do run do rust-ci.yml
RUN_RUST=<run-id>
ART_ID=$(gh api repos/Fernandes-Yuri/gateway-api/actions/runs/$RUN_RUST/artifacts \
  --jq '.artifacts[] | select(.name|test("rust|bin")) | .id' | head -1)
curl -sL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  -o /tmp/rust.zip \
  "https://api.github.com/repos/Fernandes-Yuri/gateway-api/actions/artifacts/$ART_ID/zip"
unzip -o /tmp/rust.zip -d /tmp/braga-deploy
```

> Confirme que o binário é estático linux/amd64: `file /tmp/braga-deploy/braga-chat` deve dizer
> `ELF 64-bit LSB executable, x86-64 ... statically linked`.

---

## 3. Publicar no bucket e gerar URL pré-assinada

```bash
aws s3 cp /tmp/braga-deploy/braga-chat \
  s3://braga-saude-deploy-397759110014/bin/braga-chat
aws s3 cp /tmp/braga-deploy/braga-edge \
  s3://braga-saude-deploy-397759110014/bin/braga-edge

# URL temporária (1h) — usada apenas dentro do comando SSM
aws s3 presign s3://braga-saude-deploy-397759110014/bin/braga-chat --expires-in 3600
aws s3 presign s3://braga-saude-deploy-397759110014/bin/braga-edge --expires-in 3600
```

---

## 4. Aplicar na EC2 via SSM (com backup e rollback automático)

Exemplo para o **Go** (ajuste os caminhos para o Rust):

```bash
cat > /tmp/ssm_go.json <<'EOF'
{"commands":["set -u\nBIN=/usr/local/bin/braga-chat\nSOCK=/run/braga-saude/chat.sock\nTS=$(date +%Y%m%d%H%M%S)\ncp -a \"$BIN\" \"$BIN.bak-$TS\"\ncurl -fsSL --max-time 120 \"PRESIGNED_URL\" -o /tmp/braga-chat.new\nSIZE=$(stat -c %s /tmp/braga-chat.new); echo novo_binario_bytes=$SIZE\n[ \"$SIZE\" -gt 5000000 ] || { echo FALHA_TAMANHO; exit 4; }\nchmod 0755 /tmp/braga-chat.new\ninstall -m 0755 /tmp/braga-chat.new \"$BIN\"\nsystemctl restart braga-chat.service\nsleep 3\nif ! systemctl is-active --quiet braga-chat.service; then systemctl stop braga-chat.service; cp -a \"$BIN.bak-$TS\" \"$BIN\"; systemctl start braga-chat.service; echo FALHA_SERVICO; exit 6; fi\ncurl -sf --max-time 5 --unix-socket \"$SOCK\" http://localhost/health || { systemctl stop braga-chat.service; cp -a \"$BIN.bak-$TS\" \"$BIN\"; systemctl start braga-chat.service; echo FALHA_HEALTH; exit 7; }\necho DEPLOY_OK backup=$BIN.bak-$TS"]}
EOF

CID=$(aws ssm send-command --instance-ids i-04728dd8094ed0e78 \
  --document-name AWS-RunShellScript --parameters file:///tmp/ssm_go.json \
  --timeout-seconds 180 --query Command.CommandId --output text)

sleep 15
aws ssm get-command-invocation --command-id "$CID" \
  --instance-id i-04728dd8094ed0e78 \
  --query '{Status:Status,Stdout:StandardOutputContent,Stderr:StandardErrorContent}' --output json
```

Para o **Rust**, troque o binário para `/usr/local/bin/braga-edge`, o serviço para `braga-edge.service`
e valide a saúde via HTTP público/`/ready` (a borda escuta TCP `:8080`; não há socket de saúde dedicado).

### Verificações obrigatórias pós-deploy

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.bragasaude.online/health   # 200
curl -s https://api.bragasaude.online/ready                                     # backend_sock/chat_sock ok
```

- **Go:** a rota nova/alterada deve responder pelo serviço (ex.: `POST /api/sync/family-binding`
  sem auth → `401`) e o log `journalctl -u braga-chat` não deve conter erros de inicialização.
- **Rust:** confirmar que o tráfego antigo continua funcionando (ex.: `GET /` → 200,
  `GET /api/app/latest` → 410) e que as rotas recém-adicionadas respondem como esperado.
- **Lição da Fase #6:** em produção o `braga-chat` **escuta apenas socket Unix** (não TCP `:8082`);
  valide por `systemctl` + `curl --unix-socket` + `/ready`, nunca por `curl 127.0.0.1:8082`.

---

## 5. Rollback

```bash
# Listar backups
ls -l /usr/local/bin/braga-chat.bak-* /usr/local/bin/braga-edge.bak-*

# Restaurar (parar o serviço antes para evitar ETXTBSY)
systemctl stop braga-chat.service
cp -a /usr/local/bin/braga-chat.bak-<TIMESTAMP> /usr/local/bin/braga-chat
systemctl start braga-chat.service
systemctl is-active braga-chat.service
```

Repita o mesmo padrão para `braga-edge.service`. O rollback leva menos de 1 minuto e mantém o serviço
anterior funcional.

---

## 6. Registrar no PR/commit

Sempre anexe ao PR/commit de origem (ou ao Livro-Mestre):
- run ID do artefato publicado;
- tamanho/SHA-256 do binário;
- caminho do backup criado (`*.bak-<timestamp>`);
- saída dos probes de `/health` e `/ready` (e das rotas específicas alteradas).
