# Doses por horário - contrato gateway v1

A fase 1 fornece a regra compartilhada e o protocolo para Android e WhatsApp.
Notificações agrupadas, botões de conversa e fila Room serão implementados nas fases seguintes.
O lote de tomadas é diferente do endpoint existente `/medications/batch`, que cadastra medicamentos.

## Operações

Autenticação existente da API e permissões do Care OS. O ator vem da identidade autenticada.
O WhatsApp usa a conta vinculada e chama o serviço diretamente na transação do adaptador.
IDs opacos não concedem permissão. Apenas a conta que prepara pode confirmar/recuperar o lote.

| Método | Caminho | Permissão |
|---|---|---|
| GET | `/api/medication-dose-capabilities` | Conta autenticada |
| PUT | `/api/family/patients/{patient_id}/medications/{medication_id}/dose-schedule` | medication:write |
| GET | `/api/family/patients/{patient_id}/medication-dose-groups?date=2026-10-13` | care:read |
| POST | `/api/family/patients/{patient_id}/medication-dose-batches` | medication:take |
| POST | `/api/medication-dose-batches/{batch_id}/confirm` | medication:take |
| GET | `/api/medication-dose-batches/{batch_id}` | medication:take |
| POST | `/api/medication-dose-batches/{batch_id}/cancel` | medication:take |

OpenAPI versionado: `scripts/server/openapi_care_os.json`, gerado por `generate_care_os_openapi.py`.
Novas respostas emitem `medication_batch_contract: 1`.

## Programação

```json
{
  "expected_revision": 1,
  "slots": [
    {"local_time": "08:00", "dose_units": 2, "quantity_confirmed": true},
    {"local_time": "20:00", "dose_units": 1, "quantity_confirmed": true}
  ]
}
```

A atualização devolve a nova `schedule_revision`; são aceitos até 12 horários distintos.
A quantidade é conferência humana explícita em unidades inteiras de estoque (1 a 20).
Concentração em mg não define quantidade. Formas em volume permanecem fora da confirmação coletiva.
Cadastros antigos preservam horários e não recebem quantidade confirmada na migração.
Nome, concentração, horários, forma, intervalo, duração ou contexto alterados invalidam propostas.
Reposição de estoque não altera programação, permitindo resolver pendências em uma nova proposta.

Triggers sincronizam escritores legados, incluindo PATCH e `/api/sync/medication`.
Horários removidos ficam inativos; recolocá-los exige conferir novamente a quantidade.
Horários em ambas as representações legadas são atualizados na mesma transação do PUT.

## Consulta e seleção

A data é local ao paciente, com `profiles.timezone`; ausência/fuso inválido usa
`America/Sao_Paulo`, indicado por `timezone_source: fallback`. Não usar o relógio/fuso do aparelho.
08:00 e 08:15 são grupos diferentes. Ambiguidade de horário durante transição de fuso exige revisão.
Cada dose tem ID UUIDv5 de paciente/medicamento/instante UTC; revisão não integra esse ID.
O grupo contém `schedule_digest`, contagens, `can_confirm_all` e itens visíveis pendentes/registrados/bloqueados.
A consulta materializa identidades; não registra ingestão nem baixa estoque.
`review_items` informa medicamentos cujo horário legado não pode formar um grupo válido.
Instruções cadastradas permanecem visíveis. Contextos estruturados existentes são preservados;
texto livre exige revisão, sem interpretação clínica nem subdivisão automática.

```json
{
  "client_batch_id": "11111111-1111-4111-8111-111111111111",
  "group_id": "22222222-2222-4222-8222-222222222222",
  "expected_schedule_digest": "digest SHA-256 retornado na consulta",
  "occurrence_ids": ["33333333-3333-4333-8333-333333333333"],
  "channel": "app"
}
```

Os IDs ilustrativos devem ser substituídos pelos retornados na consulta; o digest é hexadecimal de 64 caracteres.
201 cria proposta; 200 recupera a mesma intenção. ID reutilizado com outro conteúdo produz 409.
O snapshot inclui paciente, ator, fuso, data, horário, nomes, quantidades, instruções e revisões.
O conjunto exato precisa estar elegível; o servidor não reduz uma seleção sem consentimento.
Validade: cinco minutos, com `expires_at` explícito. Preparação não gera logs/baixas.

## Confirmação e recuperação

```json
{
  "confirmation_id": "44444444-4444-4444-8444-444444444444",
  "snapshot_digest": "digest SHA-256 da proposta",
  "selected_occurrence_ids": ["33333333-3333-4333-8333-333333333333"],
  "declared_taken_at": "2026-10-13T08:01:00-03:00"
}
```

Seleção não vazia, sem duplicados e contida no snapshot. O instante declarado é opcional,
exige offset e não pode estar no futuro. Ele é distinto da programação e do instante de persistência.
A seleção/ID/digest/declaração do primeiro aceite ficam fixos; reenvio idêntico recupera o resultado.
Aceite modificado de um lote resolvido resulta em 409 sem novos efeitos.

Estados: `prepared`, `confirmed`, `partially_confirmed`, `failed`, `cancelled`, `expired`.
Resultados por item: `recorded`, `already_recorded`, `excluded`, `blocked`.
Todo resultado terminal inclui itens completos, `counts` e `requires_action`.
Doses registradas por outra via são reconhecidas antes de avaliar saldo; não exigem estoque restante.
Quantidade anterior diferente produz `ALREADY_RECORDED_DIFFERENT_QUANTITY` com log/quantidade originais.
Um resultado parcial nunca deve ser apresentado como “todos registrados”.

Conflitos de estoque por item permitem resultado parcial. Alteração de cadastro/fuso,
seleção inválida, expiração ou revogação rejeitam o lote inteiro.
Erro inesperado de banco reverte logs, baixas e resultado integralmente.
A resposta só sai após commit. 503 pede recuperar o lote antes de repetir a ação.

IDs UTC, índice único das ocorrências e logs canônicos, locks ordenados e transação compartilhada
impedem segunda baixa entre app, WhatsApp e cuidadores. Triggers de inserção também
cobrem a versão anterior durante a janela entre migration e reinício do serviço. O histórico pode conter duplicações antigas;
esses logs permanecem preservados, com uma ocorrência sinalizada para revisão.
O resultado terminal fica persistido, com proteção contra reabertura no banco.

## Offline Android

1. Persistir intenção, seleção, snapshot/revisões, `client_batch_id`, `confirmation_id` e declaração antes do envio.
2. Após timeout, recuperar o lote; reenvio idêntico devolve o resultado persistido.
3. Proposta expirada requer nova preparação com novo ID e a intenção original.
4. Só reutilizar o aceite local quando membros, quantidades, instruções e revisões coincidirem exatamente.
5. Qualquer mudança exige nova apresentação ao usuário. Nunca substituir a intenção por “todos elegíveis”.
6. Manter a fila local pendente até receber resultado terminal; não anunciar confirmação remota antecipadamente.

A fila e UI pertencem à fase Android. O serviço v1 oferece a base, não implementa a fila local.
O adaptador antigo `/api/sync/medication-log` com `scheduledFor` usa agora a mesma baixa/ocorrência;
aceita o formato local legado e offsets, interpretando horário local no fuso do paciente.
O ID devolvido pode ser o log remoto já existente. Importações sem programação permanecem históricas,
sem alegar uma dose programada nem baixar estoque automaticamente.

## Erros

Novos endpoints retornam `detail: {code, message, retryable, items}`.
422 valida entrada; 403 bloqueia vínculo/permissão; 404 não revela lote de outra conta.
409 inclui `SCHEDULE_CHANGED`, `PROPOSAL_EXPIRED`, `SNAPSHOT_MISMATCH`, `IDEMPOTENCY_CONFLICT`,
`BATCH_ALREADY_RESOLVED`, `REVIEW_REQUIRED`. 503 indica indisponibilidade, nunca sucesso presumido.
Resultados parciais de negócio são HTTP 200 com estado e contagens explícitos.

## Migração e rollback operacional

`055_medication_dose_batches.sql` é aditiva, transacional e idempotente.
Horários históricos com offset são normalizados; offsets equivalentes associam logs à mesma ocorrência,
sinalizando duplicações sem apagar histórico nem ajustar estoque. Horários sem fuso ou inválidos
ficam em `medication_dose_migration_issues`; não assumir fuso histórico do servidor.
Registros locais identificáveis bloqueiam a dose até revisão, preservando a evidência.

Para rollback funcional, definir `MEDICATION_DOSE_BATCHES_ENABLED=false` e reiniciar o gateway
pelo processo operacional existente. Capacidades passam a `enabled: false`, escritas coletivas retornam
503 `CONTRACT_DISABLED`; consultas e resultados permanecem disponíveis. A confirmação individual
continua usando a proteção canônica. Reativar a flag após correção e validação.
Não desfazer a migration nem retornar ao código anterior à proteção canônica depois de produzir novas doses.
Preservar tabelas e resultados já gravados.

## Validação

A suíte PostgreSQL testa 12 doses, quantidades por horário, corrida de cuidadores/canais,
reenvio/recuperação, exclusões, falha parcial, revogação, expiração, mudança de fuso/cadastro,
rollback integral, importação legada, migração de duplicações e travas de banco.
O ensaio do runner real cobre cadeia completa, upgrade do baseline, dados sintéticos preservados,
falha deliberada e repetição idempotente. CI é gate do deploy do ambiente de ensaio.
