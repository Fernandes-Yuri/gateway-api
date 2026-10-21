# Central interna WhatsApp - Yuri Atendimento

Desenho v1, 21/10/2026. Complementa [a captação](whatsapp-clinic-intake-v1.md) e o [prompt Yuri](../prompts/whatsapp-clinic-intake-v1.txt). O Gateway já tem uma implementação inicial e o [contrato operacional HTTP/WebSocket](whatsapp-gateway-operations-v1.md), ainda sem implantação. O APK e seu repositório privado só serão criados depois de o WhatsApp e o atendente estarem funcionais e validados. Os detalhes operacionais implementados prevalecem sobre propostas de UI deste documento.

## Experiência definida

APK Android de uso interno, instalado separadamente do aplicativo dos pacientes, para o Yuri acompanhar e assumir atendimentos. O usuário do WhatsApp continua usando o mesmo número e a mesma conversa. Não precisa instalar aplicativo.

Três áreas no APK:

1. **Atendimentos**: lista com última atividade, não lidas, tipo da última mensagem e estado visível: automático, aguardando você, com você ou encerrado. Filtros por estado.
2. **Conversa**: histórico em tempo real; identificação de mensagem do contato, assistente virtual ou atendente; transcrição dos áudios; player do original; fotos ampliáveis; status de envio e botão Assumir. Ao assumir, editor de texto e gravador de áudio ficam disponíveis. Antes de enviar áudio, permitir ouvir, descartar e reenviar em caso de falha confirmada.
3. **Resumo**: dados coletados, campos desconhecidos/recusados, queixa relatada e confirmação do contato. É um resumo administrativo de acolhimento, não diagnóstico nem prontuário.

A persona automática se chama Yuri e adota atendimento direto, tranquilo e acolhedor. A identificação inicial como assistente virtual diferencia a automação do Yuri humano. Quando o operador assume, o servidor pode enviar uma única mensagem de transição: “Oi, aqui é o Yuri. Vou continuar seu atendimento por aqui.” Somente após assumir de fato, nunca em nome de operador inexistente.

## O que acontece ao concluir a coleta

1. O assistente mostra um resumo e pede confirmação. Correção reabre a coleta e invalida o resumo anterior.
2. Após confirmação explícita do resumo mostrado, o Gateway salva resumo/revisão e cria, na mesma transação, o encaminhamento e o evento de notificação.
3. O modo muda para `waiting_human`. Novas mensagens, fotos e áudios continuam entrando e aparecendo no APK, mas a LLM não responde. Não enviar repetidamente mensagens de espera a cada nova mensagem.
4. Você recebe a notificação “Atendimento aguardando sua atenção”. Ao abrir, o APK busca o resumo e o histórico no Gateway autenticado. Não colocar nome, queixa, telefone, foto ou transcrição no push ou na tela bloqueada.
5. O botão **Assumir** muda a conversa para `human` e pausa definitivamente a automação até uma ação explícita sua. Também pode ser usado antes de a coleta terminar.
6. Você responde por texto ou áudio; o Gateway envia ao WhatsApp e o APK acompanha o resultado real de entrega. O destinatário vem da conversa autenticada, não de um telefone livre informado pelo cliente.
7. Você pode encerrar ou devolver ao assistente. Voltar a `bot` não dispara resposta antiga nem reinicia a triagem do zero; a próxima mensagem é tratada com estado e histórico atuais.

Pedido de atendente humano gera o mesmo aviso imediatamente, sem exigir coleta completa. Possível urgência mantém a orientação de socorro do Gateway e cria alerta na central, sem alegar que o APK é um serviço de emergência ou garantir que o operador está disponível.

Notificação push não é garantia de leitura: salvar estado persistente `pending`, `push_accepted`, `seen` e `claimed`. Aceitação pelo provedor não equivale a você ter visto. Um item continua na fila até a ação correspondente. Se push falhar, o item e a indicação visual permanecem, e o envio será tentado novamente com limite e visibilidade da falha. O paciente só recebe afirmações compatíveis com o estado confirmado pelo servidor.

## Controle humano e prevenção de respostas simultâneas

`conversation_mode` é independente de `intake_status`. Uma conversa pode estar em modo humano com triagem incompleta. Os controles pertencem ao Gateway, não ao prompt nem à tela do APK.

| Origem | Ação | Destino | Efeito |
| --- | --- | --- | --- |
| `bot` | Assumir pelo APK | `human` | Bloqueia geração e envio automáticos |
| `bot` | Resumo confirmado ou pedido humano | `waiting_human` | Pausa LLM e cria aviso durável |
| `waiting_human` | Assumir pelo APK | `human` | Define operador e confirma tomada de controle |
| `human` | Devolver explicitamente | `bot` | Retoma somente com contexto atual |
| `waiting_human` | Devolver explicitamente | `bot` | Operador decide retomar coleta |
| qualquer modo aberto | Encerrar pelo operador | `closed` | Impede envio pendente e arquiva a sessão |
| `closed` | Nova mensagem do contato | `bot` ou `waiting_human` | Abre nova sessão conforme configuração, sem reutilizar confirmação antiga |

Toda alteração de modo incrementa `control_epoch`, registra operador e gera evento. Assumir é idempotente por chave de requisição. Se houver mais de um dispositivo, revalidar propriedade e versão; outro operador/dispositivo não pode sobrescrever controle silenciosamente.

A chamada à Groq recebe um snapshot do epoch. Antes de salvar e antes de transmitir a resposta, comparar epoch, modo e estado de exclusão. Resposta da LLM atrasada deve ser descartada. Itens automáticos de outbox ainda não enviados são cancelados na tomada de controle; novos itens humanos exigem operador autorizado, modo `human` e epoch atual.

O despacho externo e a alteração de modo precisam compartilhar a mesma exclusão mútua por conversa. Uma resposta já aceita pela Meta não pode ser desfeita. Se houver um envio em andamento, o APK mostra “Assumindo...” até o servidor resolver o envio em voo e confirmar o novo modo; não mostrar “com você” antes dessa confirmação. Uma chamada de geração não deve manter o lock durante toda a inferência; o epoch invalida seu resultado ao retornar.

Depois de assumir, manter ingestão, download de mídia, transcrição e atualização em tempo real ativos. Apenas o gerador de respostas e o emissor automático de acolhimento ficam pausados. Regras determinísticas de socorro podem permanecer ativas nos modos de espera/humano; definir uma mensagem única por evento de urgência para evitar resposta simultânea repetida.

## Áudio recebido e enviado

**Recebido do paciente:** webhook registra o identificador de mídia, worker baixa com a credencial do servidor e salva de forma privada. O APK mostra imediatamente o balão “Áudio recebido, transcrevendo...”, depois recebe um evento de transcrição concluída. O original continua acessível por player autenticado. Estado de transcrição: `pending`, `processing`, `completed` ou `failed`. Falha não apaga a mensagem: mostrar “Transcrição indisponível” com opção de ouvir e tentar novamente.

Transcrever com o serviço Whisper/Groq existente e a chave de WhatsApp do Gateway, inclusive em modo humano. Manter original e transcrição como partes da mesma mensagem, sem criar mensagens fictícias do paciente. Transcrição é resultado automático e pode conter erros; dados essenciais duvidosos são confirmados com o contato. Não gerar diagnóstico a partir do áudio.

**Enviado pelo Yuri:** gravar a voz real pelo APK, com permissão de microfone solicitada no momento do uso. Pré-escuta e confirmação antes de enviar. Upload autenticado ao Gateway; validação de tipo real, duração e tamanho; conversão somente se necessária; upload à Meta e envio de mensagem de áudio pelo Gateway. Nenhuma chave de provedor no APK, nenhuma clonagem de voz. A primeira versão pode enviar áudio como mídia de áudio comum; não prometer formato nativo de “mensagem de voz” antes de validar suporte no endpoint/versão de produção.

Validar MIME, codec e limites conforme a versão da API Meta configurada antes de implementar; não fixar limites com base em memória ou no SDK antigo. Credenciais e URLs de download do provedor nunca são devolvidas ao APK. Não registrar transcrição nem bytes em logs.

## Fotos

Receber foto, legenda e identificador da mensagem no webhook. Worker baixa a mídia e guarda em armazenamento privado com referência à conversa; anexos podem chegar antes de terminar o download. A tela mostra estado de carregamento, miniatura e imagem ampliada quando pronta.

A API interna autoriza cada acesso à mídia e entrega os bytes pelo Gateway. Não criar link público permanente nem usar diretamente a URL temporária da Meta como URL da imagem no APK. Validar tipo e tamanho, não executar conteúdo e aplicar expurgo com os dados da conversa. Fotos não são enviadas à LLM na primeira versão; não adicionar diagnóstico visual ou OCR sem escopo específico.

## Tempo real e notificações

Reutilizar a infraestrutura do Gateway: REST autenticado para comandos e sincronização, WebSocket para eventos em primeiro plano e FCM para acordar/notificar o APK em segundo plano. O sistema Android pode suspender o app; não depender de socket contínuo para receber aviso com a tela apagada.

Persistir eventos com sequência por conversa, ou cursor de commit que não pule eventos concorrentes. Conectar a partir do último cursor confirmado; reconexão consulta REST, aplica eventos idempotentemente e só depois confirma cursor. O APK não perde mensagens porque o socket caiu nem duplica balões porque recebeu push e WebSocket do mesmo evento. Cursor expirado pede snapshot atualizado, sem retornar histórico de outro usuário.

Eventos propostos: `conversation.created`, `message.created`, `message.delivery_changed`, `media.ready`, `transcript.updated`, `intake.updated`, `summary.confirmed`, `control.changed`, `handoff.updated` e `conversation.deleted`. Para exclusão, limpar mídia/cache local e remover conteúdo da tela. Push traz apenas identificador opaco e tipo de evento, e o app busca conteúdo autorizado no servidor.

## API interna proposta

Prefixo `/api/internal/whatsapp`. Todas as rotas e o stream exigem sessão privilegiada de dispositivo autorizada por TOTP mestre e revalidada pelo servidor. As rotas já estão implementadas no Gateway; tipos exatos estão no [OpenAPI v1](whatsapp-internal-v1.openapi.json).

| Rota | Função |
| --- | --- |
| `GET /conversations` | Lista paginada, não lidas e estado de controle |
| `GET /conversations/{id}` | Resumo, modo, epoch e estado do encaminhamento |
| `GET /conversations/{id}/messages?cursor=...` | Histórico paginado e sincronização |
| `POST /conversations/{id}/claim` | Assumir com epoch e chave de idempotência |
| `POST /conversations/{id}/release` | Devolver à automação explicitamente |
| `POST /conversations/{id}/close` | Encerrar e cancelar envios pendentes |
| `POST /conversations/{id}/messages` | Texto humano, condicionado a controle/epoch |
| `POST /conversations/{id}/audio` | Upload de áudio humano com idempotência |
| `GET /media/{id}` | Download autenticado, com suporte a player |
| `POST /conversations/{id}/seen` | Confirma leitura até uma sequência válida |
| `POST /devices` e `DELETE /devices/{id}` | Cadastro e remoção de dispositivo próprio |
| `GET /events` | Sincronização REST por cursor |
| `WS /events` | Stream autenticado com reconexão e cursor |

Sem token em query string do socket. Preferir autenticação no handshake por header do cliente Android; expiração de sessão encerra/reautentica a conexão. Autorização revogada precisa bloquear REST, stream e mídia, inclusive sockets já abertos e entregas de push posteriores à revogação. Não reutilizar token administrativo de serviço dentro do APK.

`POST` humano responde com item `queued`; a tela diferencia fila, envio aceito, entrega, leitura e falha. Não marcar entregue por ter recebido HTTP 200 do Gateway. Repetição pelo usuário só é permitida com a mesma chave de idempotência ou como novo envio explícito após falha confirmada; casos de aceite externo ambíguo pedem reconciliação.

## Identidade e distribuição do APK

Aplicativo separado, com nome “Yuri Atendimento” e identificador próprio proposto `br.com.bragasaude.atendimento`. Cadastro de dispositivo somente com o mesmo TOTP mestre do portal administrativo. As sessões são emitidas pelo Gateway e podem ser renovadas/revogadas, sem conta de paciente e sem Firebase Auth para o operador. Firebase/FCM será usado apenas para as notificações do novo aplicativo.

O APK envia um código temporário para `/auth/enroll`; nunca recebe o segredo TOTP, as chaves de provedor ou o token de serviço do portal. O limite de tentativas e o bloqueio de reuso de código são compartilhados com o portal. Depois do cadastro, usa sessão opaca curta e refresh token rotativo, guardados com proteção do Android Keystore. O [contrato operacional](whatsapp-gateway-operations-v1.md) descreve os corpos e a validade exatos.

APK assinado distribuído diretamente para uso interno. O projeto Android atual proíbe Gradle neste notebook; usar GitHub Actions para builds e validação de compilação, sem reutilizar o identificador do app dos pacientes para esta central. Alterações externas, criação de credenciais e distribuição ocorrem depois de existir implementação revisável, dentro das autorizações de cada etapa.

## Etapas de implementação e verificação

1. Backend: inbox/outbox, armazenamento de mensagens/anexos, controle com epoch, captação com prompt Yuri, resumo confirmado, autorização de operador e notificação durável.
2. Testar antes da UI: tomada de controle durante geração e envio; autorização de todos os endpoints/mídia/stream; idempotência; reinício; transcrição em modo humano; push falhando; exclusão em corrida com worker.
3. APK mínimo: login, lista, conversa em tempo real, resumo, Assumir/Devolver/Encerrar, texto, player/transcrição, gravador e visualização de fotos.
4. Validar em aparelho com a tela apagada, app fechado, rede oscilando, microfone negado e expiração de login; confirmar que só o dispositivo autorizado recebe o aviso.
5. Ponta a ponta em conversa controlada: paciente escreve, envia áudio/foto, confirma resumo, Yuri recebe aviso, assume e responde por texto/áudio sem resposta concorrente da LLM.

Pendências operacionais antes da ativação: presença do TOTP mestre do portal; prazos de retenção; configuração Firebase/FCM do aplicativo separado; funcionamento/codec dos áudios com a versão Meta configurada. Este desenho não altera a produção nem envia mensagens de teste para pacientes.

## Referências

- Cadastro administrativo: mesmas funções de TOTP mestre de `portal_auth_security.py` já usadas no portal.
- A autorização do operador é revalidada no Gateway, inclusive no stream; a tela do aplicativo não concede acesso.
- [Meta: envio de áudio](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/messages/audio/). Referência conceitual do SDK arquivado; confirmar formato e limites na documentação da versão Cloud API de produção durante a implementação.
