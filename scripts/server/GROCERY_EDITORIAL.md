# Edições semanais: primeira entrega do webservice

O fluxo cobre criar/duplicar edição, pesquisar preços, montar packs de um único
mercado, comparar, revisar e publicar uma versão fechada. O Android consulta o
catálogo publicado e mantém a lista adotada independente de atualizações.

## Fluxo do operador

No dashboard, abra **Lista de compras → Edição semanal**.

1. Cadastre os produtos/embalagens e os supermercados nas abas de catálogo.
2. Crie uma edição com nome, início/fim da validade, região/cidade/CEP e notas.
3. Em **Pesquisar**, clique na célula produto × mercado. Registre preço por
   embalagem, link, data/hora, condições, mínimo de compra e exigência de clube.
   Marque “Conferi este preço para esta edição” depois da pesquisa manual.
4. Em **Montar packs**, escolha um único mercado, nome, descrição, capa opcional
   e produtos/quantidades. Um pack pode ser uma cesta completa da seleção ou uma
   categoria. Pode ser salvo incompleto para continuar depois.
5. Em **Revisar e visualizar**, gere a prévia: composição, total/subtotal,
   pendências, fontes, datas, condições e comparação da mesma cesta em outros
   mercados. A tela sinaliza expressamente que não está publicada.
6. Para outra semana, use **Duplicar para outra semana**. Packs recebem novos
   identificadores; preços, fontes e datas são copiados, mas todas as pesquisas
   ficam a conferir. Nenhuma cópia ganha data de pesquisa nova automaticamente.

O catálogo base continua compartilhado; pesquisas e composição dos packs são
isoladas por edição. A prévia reflete o catálogo atual. A publicação congela uma cópia dos metadados por versão; novas edições do
catálogo não alteram versões já publicadas.
Trocar entre catálogo e bancada na mesma tela preserva a seleção da edição.
Campos só persistem depois do botão de salvar correspondente.

## Regras da prévia

- Cada pack tem exatamente um mercado. Os itens não aceitam mercados individuais.
- Produtos inativos, mercado inativo, pack vazio e categoria incompatível geram
  pendências. Itens repetidos e quantidades fracionárias são rejeitados.
- Cotação não conferida, sem fonte, ou abaixo do mínimo da oferta não entra no
  total conferido. O valor parcial aparece como subtotal.
- Concorrentes são comparados pela mesma embalagem e quantidade. Rankings
  completos vêm primeiro; subtotais incompletos não recebem destaque de menor
  total. Empates completos são assinalados.
- Para afirmar comparação completa é necessário o mercado do pack mais pelo
  menos um concorrente com todos os itens conferidos. Ofertas condicionais são
  exibidas como tais, incluindo clube e quantidade mínima.
- Região e validade pertencem à edição. Condições livres não são interpretadas
  automaticamente: cabe à revisão editorial verificar datas promocionais,
  filiais, frete e demais restrições antes de uma futura publicação.
- A energia exibida é o conteúdo das embalagens, não a cobertura de consumo de
  uma dieta semanal. “Cesta completa” é a seleção comercial do operador.

## Persistência e API

Aplique a migração `059_grocery_editorial_workspace.sql` após a 058, pelo fluxo
habitual do projeto. O rollback 059 remove somente as quatro tabelas editoriais.

- `grocery_editions`: datas, região, notas, revisão e edição de origem.
- `grocery_edition_quotes`: pesquisa isolada por edição/produto/mercado.
- `grocery_editorial_packs`: mercado único, tipo e apresentação do pack.
- `grocery_editorial_pack_items`: composição, quantidade e ordem.

As pesquisas semanais não escrevem em `grocery_market_prices`. Os endpoints
públicos existentes do Android permanecem independentes do rascunho.

Endpoints administrativos no gateway:

- `GET/POST /api/admin/grocery/editions`
- `GET/PUT /api/admin/grocery/editions/{id}`
- `GET /api/admin/grocery/editions/{id}/preview`

O webservice usa o proxy autenticado `/api/admin/grocery-editions` com os mesmos
sufixos. Nenhum endereço de gateway arbitrário é aceito pelo proxy.
PUT exige `{expected_revision, operation, payload}`. Operações: `metadata`,
`quote`, `remove_quote`, `pack`, `remove_pack`. O servidor bloqueia a edição na
transação, verifica a revisão e incrementa-a depois da gravação. Revisão antiga
retorna 409; o painel preserva os campos e pede atualização explícita.
Tentativas de alterar/remover um pack por outra edição retornam 404.

## Verificação

```bash
# gateway-api, sem serviços externos
PYTHONPATH=.:scripts/server python3 -m pytest -q \
  scripts/server/test_grocery_editorial.py scripts/server/test_migrate_runner.py

# webservice
npm test
npx tsc --noEmit --incremental false
```

`test_grocery_editorial_postgres.py` usa somente o PostgreSQL efêmero local
`braga_privacy_test` do CI, via `TEST_POSTGRES_URL`. Verifica migrações,
duplicação, isolamento, revisão concorrente e rollback de alteração parcial.
Sem esse banco o teste é explicitamente pulado.

Validação manual sugerida: dois mercados, um pack completo e um de categoria;
pesquisar preços, fechar/reabrir a edição, comparar, duplicar e confirmar que as
pesquisas da cópia voltaram a conferir e a edição original não foi alterada.


## Publicação versionada e distribuição

A migração `060_grocery_editorial_publications.sql` sucede 058 e 059. Armazena
snapshots imutáveis, uma referência de versão selecionada por edição e uma
revisão global da vitrine. Um trigger impede UPDATE/DELETE dos snapshots;
retirada e restauração alteram somente a referência vigente. O rollback 060
remove exclusivamente essas estruturas de publicação e preserva os rascunhos.

Na revisão, selecione os packs conferidos e use **Publicar packs revisados**.
A requisição inclui revisão do rascunho, versão anteriormente selecionada,
identificador idempotente e digest da prévia. Alteração no rascunho ou catálogo
após a revisão retorna 409 e exige nova prévia. Packs pendentes não são
publicáveis. Cada versão congela embalagens, imagens, preços, mercado,
condições, composição e comparativos; observações privadas da edição não são
incluídas. Publicar uma nova versão substitui de uma só vez os packs dessa
edição na vitrine, incluindo a retirada dos packs não selecionados.

O histórico permite **Retirar da vitrine** e **Restaurar esta versão** sem
reescrever o snapshot. Não é permitido restaurar uma versão já vencida. Versões
com validade futura ficam agendadas e não aparecem no catálogo público até o
início da validade. Datas seguem America/Sao_Paulo.

Rotas administrativas adicionais: GET/POST/PUT
`/api/admin/grocery/editions/{id}/publications`. POST publica, PUT retira/restaura.
O proxy webservice usa o mesmo sufixo em `/api/admin/grocery-editions/{id}`.

Rotas públicas, sem conteúdo de rascunho:

- `GET /api/catalog/grocery/publications`: versões selecionadas e válidas.
- `GET /api/catalog/grocery/publications/revision`: revisão global e data local.

As respostas não permitem cache HTTP. O Android consulta a revisão ao abrir ou
retomar a vitrine e a cada 15 segundos enquanto a tela estiver visível. Só
baixa o conteúdo completo quando a revisão ou data local muda. Não utiliza
WebSocket/SSE ou push; offline conserva a última vitrine salva e avisa sobre a
falta de conexão. Packs vencidos são ocultados mesmo offline. Retiradas feitas
durante a desconexão só podem ser conhecidas após reconectar.

O Room 56 mantém cache e adesão separados por usuário. Iniciar um pack exige
uma conferência online do catálogo e confirmação explícita de substituição da
lista; estoque previamente registrado permanece. Atualizar, retirar ou
restaurar publicação nunca altera a lista adotada nem suas marcações. A tela
avisa quando a publicação dessa lista não corresponde à vitrine atual. A nova
lista usa preços do mercado congelado, não o preço médio genérico do produto.
Adições manuais/geração automática ficam bloqueadas nessa lista para manter o
mercado único; é possível remover itens, marcar compras ou iniciar outra lista.

Verificações adicionais: `test_grocery_publications.py` e
`test_grocery_editorial_postgres.py` no backend, testes de proxy em
`portal-auth.test.cjs`, e `PublishedGroceryPackPersistenceTest` no Android.
Build, migração Room e testes Android devem rodar no GitHub Actions, conforme
AGENTS.md; não executar Gradle neste ambiente.
