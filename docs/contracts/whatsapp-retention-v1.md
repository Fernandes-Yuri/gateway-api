# Guarda curta do atendimento WhatsApp

Decisão do responsável: 23 horas e 59 minutos (86.340 segundos) desde a abertura da conversa, não desde a última mensagem. O prazo não é prorrogado por interação humana ou automática. Após o expurgo, uma próxima mensagem poderá abrir outro atendimento; o histórico anterior não será recuperado para a central.

## Cláusula para o aceite

> O histórico ativo deste atendimento, incluindo mensagens, transcrições, fotos e áudios, fica disponível por até 23 horas e 59 minutos desde a abertura da conversa, sem prorrogação por novas mensagens. Ao vencer esse prazo, o acesso é bloqueado e o histórico entra na rotina de exclusão automática. Essa rotina é executada periodicamente e pode sofrer atraso durante indisponibilidade. Cópias de segurança seguem ciclo próprio e não são eliminadas individualmente no mesmo instante. As mensagens guardadas no seu WhatsApp e em seus backups não são apagadas por esse procedimento.

O aviso determinístico do Gateway comunica esse prazo e diferencia backups. A revisão do aviso é identificada por 2026-10-23h59. O termo elaborado permanece em revisão no repositório privado braga-atendente, sem publicação automática da versão completa no site.

## Aplicação técnica

- Política fixa, com variável legada WHATSAPP_INTAKE_RETENTION_DAYS ignorada.
- API oculta lista/detalhe/mensagens/mídia vencidos e rejeita comandos; a proteção independe de o worker já ter apagado as linhas.
- Worker tenta apagar a cada 60 segundos com cascata: conversa, mensagens, anexos, inbox, comandos, handoffs e leitura. Eventos anteriores são apagados e um marcador conversation.deleted sem conteúdo é emitido. O marcador possui o mesmo prazo desde sua própria criação para atualizar clientes desconectados.
- Eventos em geral expiram após 23h59 desde a criação. Registro mínimo de aceite e metadados de sessão têm política separada e ainda exigem definição de expurgo; não contêm o histórico da conversa.
- Backup RDS tinha retenção configurada de 1 dia na auditoria de 21/10/2026, com snapshots automáticos mais antigos ainda disponíveis. Esta alteração não muda backups nem promete apagamento físico de toda cópia em 23h59.
- Não apagar dados de segurança ou registros clínicos de outros módulos por essa rotina.

## Verificação

Integração em PostgreSQL: histórico/mídia visível antes do limite e indisponível após o limite mesmo com updated_at recente; comandos vencidos rejeitados; mídia e inbox removidas por cascata; variável legada de 30 dias não estende o prazo; conversa recente preservada mesmo com updated_at antigo; eventos antigos removidos. Suíte de contratos e concorrência: 30 testes e 5 subtestes passaram. OpenAPI continua compatível com as rotas.
