# Catálogo comercial da lista de compras

As rotas públicas `GET /api/catalog/grocery/cards?category=...&q=...` e
`POST /api/catalog/grocery/basket-comparison` usam a migração
`058_grocery_retail_cards_and_markets.sql` (049 já está ocupada).
Aplique a migração pelo fluxo habitual do projeto antes de disponibilizar o Android.
O rollback remove somente as três tabelas comerciais novas.

O catálogo é editorial: não contém cotações, fotos ou marcas inventadas.
Cadastre redes e produtos com fontes verificadas. Não há rede regional fixada no APK.
O app começa com a cesta existente ou vazia; as sugestões automáticas continuam opcionais.

## Administração pelo webservice

A aba **Lista de compras → Edição semanal** é o espaço principal de pesquisa e
montagem manual dos packs. Consulte [GROCERY_EDITORIAL.md](GROCERY_EDITORIAL.md)
para o fluxo da primeira entrega, as regras e os endpoints privados.

**Catálogo de produtos** e **Supermercados** mantêm os cadastros reutilizáveis:
nomes, imagens por URL HTTPS com prévia, embalagens, informações nutricionais,
logos e lojas. **Ingredientes e preços de referência** preserva os controles
canônicos anteriores, independentes das pesquisas semanais.

As APIs `/api/admin/grocery/retail` e `/api/catalog/grocery/basket-comparison`
continuam compatíveis com o fluxo anterior; a nova bancada usa cotações isoladas
por edição e não altera esses preços públicos ao salvar um rascunho. A
publicação editorial usa os snapshots versionados descritos no documento acima.

## Importação opcional em lote

Crie um JSON com `markets`, `cards` e `prices`, conforme
`schemas/grocery_cards.py`. O arquivo `grocery_cards.template.json` é um ponto de
partida vazio. Use URLs HTTP(S) para fotos, logos e lojas.

- `markets`: `id`, `name`, `logo_url`, `website_url`, `active` (padrão true).
- `cards`: `id`, `name`, `category`, `image_url`, `package_display`,
  `package_net_value`, `package_unit` (g, ml ou un), `kcal_per_package`,
  `carbs_g`, `protein_g`, `fat_g`, `price_avg`, `linked_ingredient_slug`,
  `clinical_tags` e `active`.
- `prices`: `card_id`, `market_id`, `price`, `product_store_url` opcional,
  `last_updated` obrigatório em ISO 8601 com fuso (data real da pesquisa).

Calorias e macronutrientes são **totais da embalagem**, não por 100g nem por
porção. Preços são BRL por embalagem, com duas casas. `price_avg` é a referência
editorial da embalagem, independente das cotações por rede. Não trate ausência de
cotação como preço zero. IDs de produto devem representar uma embalagem estável.
Use `active=false` para retirar um card/rede e preserve IDs históricos.
Campos omitidos de um registro são redefinidos para os padrões do schema;
registros inteiramente omitidos do lote são preservados.

A ligação com ingrediente aceita somente a mesma dimensão: g para kg, ml para L,
un para un. Não pressupõe densidade nem peso unitário.
As tags clínicas devem ser revisadas pelo responsável pelo catálogo; sem a tag
correspondente o Android não inclui o produto na vitrine filtrada para diabetes
ou hipertensão. Não cadastre tags de segurança por dedução do nome comercial.

Gere o SQL validado, revise e aplique pelo fluxo administrativo existente:

```bash
python3 scripts/server/seed_grocery_cards.py catalog.json > /tmp/grocery.sql
```

O utilitário não conecta ao banco. O SQL faz upsert numa transação e preserva a
data informada da cotação. A importação de produtos, mercados e preços dispensa
recompilar o APK. Fotos são carregadas por Coil; os metadados persistem em Room
para consulta offline, mas a disponibilidade offline das imagens depende do cache.

## Comparação e limitações

Payload: `{"items":[{"card_id":"id-do-produto","quantity":2}]}`.
IDs desconhecidos/inativos retornam 422. Quantidades são inteiros positivos até
999, incluindo a soma de IDs repetidos. Cesta vazia retorna rankings vazios.
Só cestas completas disputam melhor preço e economia; empates ficam assinalados.
`missing_items_count` conta produtos distintos sem cotação, não embalagens.
Totais parciais são exibidos como subtotais. Não inclui frete, região ou promoções
condicionais. A data da pesquisa acompanha cada cotação no catálogo.

Itens legados sem kcal por embalagem são sinalizados como cobertura parcial e
ficam fora do comparativo comercial. A meta de compra representa o conteúdo das
embalagens, separadamente do consumo planejado no resumo legado. Alterar a meta
calórica não gera nem substitui a cesta. Produtos marcados como comprados exigem
ser desmarcados antes de alterar quantidades, para preservar o saldo da despensa.

## Verificação

```bash
cd scripts/server
python3 -m pytest -q test_grocery_cards.py test_basket_comparison.py
```

No Android, `GroceryWeeklyGoalTest` cobre meta e embalagens;
`GroceryRetailPersistenceTest` cobre cache, reabertura do Room, normalização e
estorno de estoque. Execute build/testes e exportação do schema Room 55 no GitHub
Actions, conforme AGENTS.md do app. Gradle local é proibido neste ambiente.
