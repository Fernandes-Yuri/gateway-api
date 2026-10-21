# Gateway WhatsApp Yuri - implementação e operação v1

A implementação está no Gateway API. Não há novo repositório nem APK nesta etapa. O fluxo deve passar pela validação controlada do WhatsApp antes de iniciar o aplicativo interno.

## Código e contrato

- `scripts/server/routers/whatsapp_intake.py`: assinatura Meta, limite de corpo de 1 MiB e, com captação habilitada, commit da inbox antes do ACK.
- `scripts/server/services/wa_intake_worker.py`: ingestão recuperável, aceite, mídia, transcrição, prompt Yuri, resumo, envio e notificação.
- `scripts/server/services/wa_intake_store.py`: estado, deduplicação, controle humano e eventos transacionais.
- `scripts/server/services/wa_intake_transport.py`: Meta/Groq e validação de mídia, sem segredos ou URLs temporárias no contrato público.
- `scripts/server/routers/whatsapp_internal.py`: API interna, consulta por cursor e WebSocket.
- `migrations/068_whatsapp_intake.sql`: tabelas; rollback separado. É independente do canal de saúde retirado pela migration 066; não recria vínculos no aplicativo. As tabelas de limites e uso único do TOTP são as mesmas do portal (028).
- [OpenAPI HTTP v1](whatsapp-internal-v1.openapi.json): gerado das rotas com `python scripts/export_whatsapp_contract.py`. O CI verifica divergência.

A captação vem **desativada** em `.env.example`. O cadastro de dispositivo exige o mesmo TOTP mestre usado para autorizar cadastros no portal (`ADMIN_TOTP_SECRET`). O Gateway reutiliza `master_step`, `consume_master` e os limites globais do portal: um mesmo código não pode ser reutilizado entre os dois serviços. Nenhum usuário de paciente ou UID Firebase concede acesso. O segredo TOTP fica exclusivamente no servidor.

## Contrato para o APK futuro

Prefixo `/api/internal/whatsapp`. `POST /auth/enroll` recebe `device_id` (UUID), `display_name` e `master_totp_code` de seis dígitos. Retorna sessão opaca: `access_token` válido por 15 minutos, `refresh_token` com prazo absoluto de 30 dias e `operator_uid` vinculado ao dispositivo. O APK deve guardar esses tokens em armazenamento protegido por Android Keystore; nunca armazenar o segredo mestre. `POST /auth/refresh` recebe `refresh_token`, rotaciona ambos os tokens e invalida o par anterior. `POST /auth/logout` revoga a sessão, o push daquele dispositivo e devolve seus atendimentos à fila humana. As demais rotas usam `Authorization: Bearer <access_token>`, inclusive o handshake WebSocket; nunca token em URL. Trocar/remover o segredo mestre invalida as sessões existentes. O cadastro e a renovação têm limite persistente de tentativas. Respostas de erro usam `{"detail":"..."}`. Códigos relevantes: 401 sessão ausente/inválida, 403 acesso negado, 404 recurso inexistente, 409 conflito de controle/idempotência/janela/anexo pendente, 410 cursor expirado, 422 corpo ou mídia inválidos, 503 dependência indisponível.

- `GET /capabilities`: versão, modo habilitado, disponibilidade de validação de áudio e limites da aplicação.
- `GET /conversations?limit=30&offset=0&mode=waiting_human`: lista ordenada pela última atividade, resumo do encaminhamento, não lidas por operador, próxima posição e `event_cursor` consistente com o snapshot.
- `GET /conversations/{id}`: modo, `epoch`, operador atual, campos e resumo, aceite e último encaminhamento.
- `GET /conversations/{id}/messages?cursor=0&limit=50`: mensagens em ordem crescente, inclusive origem, entrega, transcrição e identificador/estado/MIME de mídia. O cursor é o `id` da última mensagem. Atualizações posteriores chegam por eventos e exigem recarregar a conversa/histórico; não aparecem como mensagem nova fictícia.
- `POST /conversations/{id}/claim`, `/release`, `/close`: corpo `{"epoch":0,"idempotency_key":"chave-unica-do-comando"}`. Retornam conversa, novo epoch e modo. Repetir a mesma chave/corpo devolve o resultado original; chave reutilizada para outra ação ou payload conflitante retorna 409. O cliente deve consultar o estado atual após reconexão, sem inferi-lo de uma resposta idempotente antiga.
- `POST /conversations/{id}/messages`: mesmo controle, acrescido de `text`; exige modo humano e operador atual. Retorna 202 com `message_id` e `delivery=queued`. Não equivale a entregue.
- `POST /conversations/{id}/audio`: multipart com `epoch`, `idempotency_key` e `file`. Mesma regra de controle e entrega. O Gateway valida o áudio antes de enfileirar; nunca confiar apenas no MIME informado.
- `GET /media/{id}`: bytes autenticados, `Cache-Control: no-store`, suporte a intervalo único `Range: bytes=...` (206/416) para player. Não devolve chave, telefone de destino ou URL Meta.
- `POST /conversations/{id}/seen`: corpo `{"message_id":123}`; confirma leitura até mensagem pertencente à conversa; 204.
- `POST /devices`: corpo `{"device_id":"UUID","token":"token-FCM-do-dispositivo"}`; 204. `DELETE /devices/{device_id}` remove somente dispositivo do operador autenticado; 204. O token nunca é devolvido em consultas.
- `GET /events?cursor=0`: página de eventos; `WS /events?cursor=0`: o mesmo envelope em tempo real, inclusive página vazia como heartbeat. Não há endpoint `/ws` separado.

O OpenAPI contém os tipos e campos exatos. Envelope de eventos: `items` com `id`, `conversation_id`, `kind`, `data`, `created_at`; `next_cursor`; `has_more`. Cada evento informa que um recurso mudou; o cliente busca o conteúdo nas rotas autenticadas. Tipos atuais: `conversation.updated`, `message.created`, `message.delivery_changed`, `media.ready`, `transcript.updated`, `intake.updated`, `control.changed`, `summary.confirmed`, `handoff.updated`, `conversation.deleted`.

No primeiro acesso, listar atendimentos e guardar o `event_cursor` do snapshot; consumir eventos posteriores e mesclar por identificador. A paginação da lista usa offset e pode se reorganizar com atividade nova: atualizar a lista após eventos, sem usar o offset como cursor de tempo real. Durante a carga de várias páginas, preservar o cursor do primeiro snapshot até iniciar a aplicação dos eventos; não saltar diretamente para o cursor de uma página posterior. O histórico tem cursor próprio por mensagem. Persistir cursor do stream só após aplicar sua página. `conversation.deleted` apaga os dados dessa conversa do dispositivo.

O WebSocket verifica a sessão opaca, a habilitação do operador, a expiração e o vínculo com o TOTP mestre atual a cada ciclo. Fecha com código 1008 e motivo `authorization_required` ou `resync_required`. No primeiro caso, renovar login antes de reconectar; no segundo, descartar cursor, buscar snapshot e retomar. Cabeçalho de autorização deve ser enviado no handshake do cliente Android.

## Controle e falhas

`epoch` muda em cada troca de controle. A inferência ocorre fora do lock, e seu resultado só é aplicado se modo e epoch continuarem válidos. Um envio externo em andamento mantém lock da conversa: a tomada de controle espera terminar esse envio e só então retorna confirmação. O APK deve mostrar “Assumindo...” até receber essa confirmação.

Uma saída `sending` interrompida vira `uncertain`, sem reenvio automático. Timeouts de envio e respostas externas ambíguas também ficam `uncertain`; o operador pode verificar o histórico antes de um novo envio explícito. Uma rejeição confirmada por quota permite backoff com limite de tentativas. Não há garantia de entrega exatamente uma vez nem reconciliação automática com uma mensagem que a Meta aceitou antes de uma perda de conexão.

Outbox de mensagens é a própria tabela `whatsapp_intake_messages`; a coluna `delivery` e os dados de mídia tornam o despacho recuperável. Entrada, geração e saída têm workers separados: uma inferência lenta não impede tomar controle. Transcrição continua em `human` e `waiting_human`. Falha de modelo/contrato encaminha à fila humana em vez de inventar resposta. Falha de transcrição preserva áudio e mostra estado `failed`. Anexos sem download válido ficam `failed`.

Após confirmação de resumo efetivamente enviado, cria-se um encaminhamento durável e a conversa entra em `waiting_human`. Sem APK/dispositivo cadastrado, o aviso permanece como item pendente consultável pela API; não existe promessa de push recebido. O worker de push tenta por até dez ciclos, e uma falha/esgotamento continua visível como `pending`. `push_accepted`, `seen` e `claimed` são estados distintos. Push não contém dados clínicos, nome ou telefone.

Enquanto houver atendimento humano, socorro determinístico pode ser enviado para texto/áudio reconhecido como possível urgência; isso não devolve o controle à LLM. Pedidos normais e mensagens de espera não recebem resposta automática em modo humano.

## Pré-requisitos de ativação

1. Aplicar migrations pelo runner do projeto, revisando somente as mudanças da entrega; não aplicar alterações de outras frentes do workspace por acidente. Instalar código/asset de prompt e reiniciar pelos procedimentos normais do Gateway.
2. Reutilizar variáveis Meta e Groq existentes na EC2. `WHATSAPP_APP_SECRET` já foi verificado como presente na EC2. Nenhum valor foi copiado para o repositório.
3. Disponibilizar os executáveis **ffprobe** e **ffmpeg** no host. Podem ser apontados por `WHATSAPP_FFPROBE_BIN` e `WHATSAPP_FFMPEG_BIN`. A validação inspeciona tipo real, codec, canais e duração; AAC/AMR recebidos são convertidos para WAV antes da Groq. Não é necessário instalar bibliotecas Python adicionais. Áudio fica indisponível sem essa dependência; `/capabilities` informa a ausência de ffprobe.
4. Reutilizar `ADMIN_TOTP_SECRET` já usado pelo portal. Não criar UID de paciente, allowlist de e-mail ou novo segredo mestre. O cadastro do APK ocorrerá por `/auth/enroll` usando o código do autenticador, quando o cliente existir. Até lá a fila humana permanece consultável por sessão administrativa autorizada.
5. Definir `WHATSAPP_INTAKE_RETENTION_DAYS` (padrão inicial: 30; faixa 1-365). Payload bruto da inbox expira após uma hora; conversas sem atividade expiram pelo prazo configurado, com cascata para mensagens, mídia, comandos e encaminhamentos. Eventos têm retenção de 30 dias. Exclusão explícita do canal expurga os mesmos dados e publica tombstone para a central. Backups precisam seguir a política operacional do banco.
6. Habilitar `WHATSAPP_INTAKE_ENABLED=true` no processo e reiniciar **somente quando o número puder operar como captação**. Desabilitado, o webhook retorna 404. Não há reativação do antigo assistente de saúde. O novo fluxo não exige vínculo com aplicativo de paciente; exige aceite do canal. O modo é por número, não inferido da mensagem inicial do site.
7. Validar numa conversa autorizada: desafio/assinatura, texto, áudio real, foto real, resumo, fila humana, claim e resposta de operador por API. Push em aparelho só poderá ser validado após existir cliente interno e dispositivo cadastrado; até lá, usar o estado durável da API como confirmação do encaminhamento.

Não foram criados recursos AWS, alteradas credenciais, aplicadas migrations de produção ou habilitado o novo modo nesta implementação. Os executáveis de mídia e a presença das variáveis de provedor/TOTP são conferidos na ativação. O cadastro do APK será feito depois, com TOTP, sem depender de conta de paciente.

## Limites da primeira versão

A central acompanha o fluxo de captação implementado neste módulo. Mensagens emitidas por integrações legadas independentes não são retroativamente importadas. Não há recuperação do histórico anterior à ativação. Fotos são disponibilizadas ao operador, sem análise visual pela LLM. Áudios enviados pelo operador são mídia de áudio, sem promessa de exibição como voice note nativa. A aplicação limita áudio a 16 MiB e dez minutos e imagem a 5 MiB; a Meta continua podendo rejeitar mídia conforme seu contrato.

A API retorna respostas de entrega, mas não substitui os webhooks de status da Meta. Resposta livre fora da janela de 24 horas é bloqueada; não iniciar conversa por template na primeira versão.

## Verificação

Testes em `test_whatsapp_intake.py` e `test_whatsapp_intake_postgres.py`, com PostgreSQL efêmero e provedores simulados. Há também teste de contrato real da Groq com dados fictícios realizado durante o desenvolvimento, sem conversa de paciente ou envio de WhatsApp. O workflow `Contratos WhatsApp Yuri` executa integração e confere o OpenAPI. Regressões do WhatsApp anterior e privacidade são verificadas separadamente.

A ativação nesta entrega termina na configuração e prontidão do serviço. O teste pelo WhatsApp real e de push em aparelho fica reservado à etapa do APK, conforme decisão do responsável.
