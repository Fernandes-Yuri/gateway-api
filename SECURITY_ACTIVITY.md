# Atividade suspeita recebida pela API

A migração 036 cria `security_request_events`. O middleware coleta requisições HTTP `/api/`, incluindo recusas, e persiste lotes em segundo plano. Não captura WebSocket nem eventos bloqueados antes do gateway. Rotas administrativas são excluídas para evitar ruído, exceto login e segundo fator quando encaminhados com contexto assinado pelo portal.

Cada evento contém UUID próprio, horário, IP, UID autenticado pelo Firebase quando disponível, país/região informados pela borda, template da rota, método, status, duração e sinal. Não contém corpo JSON, query, token, senha, nome clínico ou UID extraído do caminho. Localização é aproximada e pode estar ausente; versão do app é declarada, não comprovada.

O portal assina metadados de IP com o token de serviço existente, expiração curta e escopo por método/rota. O IP do visitante permanece separado do IP da conexão do servidor. Sem prova do proxy, o portal informa `unknown`. Não é necessária chave Cloudflare.

## Sinais e pontuação inicial

UID divergente, recurso sem autorização e campo reservado ao servidor no perfil valem 8 pontos por evento. Payload inválido e credencial inválida valem 2; operação não autorizada e limite excedido valem 3; recurso inexistente vale 1. Tráfego aceito, autenticação ausente, restrição ativa e falha do serviço valem zero.

O peso decai linearmente até zero após 15 minutos. A soma por IP ou UID é normal abaixo de 20, atenção a partir de 20 e alta a partir de 60. São parâmetros iniciais para calibrar com tráfego real. Volume e pico por minuto são contexto, não pontuação. IP compartilhado exige analisar os UIDs antes de bloquear.

O perfil recusa apenas campos conhecidos reservados a sessão/administração. Outros campos desconhecidos continuam compatíveis com versões antigas do app. Um JSON inválido não comprova uso de Burp Suite nem identifica um atacante.

## Consulta e operação

`GET /api/admin/security-activity` aceita `groupBy=ip|uid`, `minutes=15..1440` e `limit=1..200`. Retorna grupos com algum sinal de peso positivo, inclusive quando o risco já decaiu. `GET /api/admin/security-events` aceita os mesmos período/limite e filtros opcionais `ip` e `userId`. Ambas exigem token de serviço, identidade administrativa assinada e role admin/superadmin.

Não há novo bloqueio automático. A interface reutiliza bloqueio de IP e moderação de UID existentes, com motivo e auditoria. Logs começam após a publicação; não há importação histórica da Cloudflare.

Retenção: 7 dias, limpeza horária. Fila em memória: até 2.000 eventos por processo, lotes de 100 a cada 2 segundos. Fila cheia ou falha de banco pode perder eventos; perdas e erros aparecem no painel. A fila não é durável e contadores reiniciam junto com o processo. Persistência e limpeza possuem tempo limite.

Publicação exclusivamente pelo GitHub Actions em master. O CI executa testes com PostgreSQL real para validar migração, consultas, decaimento e tráfego legítimo compartilhado.
