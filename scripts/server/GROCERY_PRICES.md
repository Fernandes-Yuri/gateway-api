# Preços de alimentos por IPCA

O job de segunda-feira às 03:00 America/Sao_Paulo usa a API pública SIDRA,
tabela 1737, variável 63. Não consulta MELI e não exige nova chave ou pacote.
O preenchimento inicial usa a consulta HTML pública da CEAGESP e o PDF mensal
PROCON-SP/DIEESE. CONAB e outras fontes ainda não têm integração verificada.

## Baseline automático

Na inicialização, após o seed, uma tarefa em background preenche somente
preços nulos. O job semanal também tenta as fontes para itens ainda nulos.
Registros com preço e registros manuais nunca são substituídos pelo coletor
de baseline. A gravação protege alterações concorrentes por `updated_at`.

`grocery_source_map.json` registra correspondências explícitas com os termos
comerciais do seed, unidade de destino, quantidade e fator. Não há fuzzy
matching nem equivalência de peixe inteiro com filé ou de arroz branco com
integral. Na consulta real de 06/10/2026, foram obtidos 70 baselines de 220
itens: 69 CEAGESP e 1 PROCON. Os 150 restantes continuam pendentes, até que
uma fonte e uma conversão verificáveis sejam acrescentadas ao mapa.

CEAGESP: seleciona datas publicadas de até 7 dias, consulta cinco categorias,
verifica cabeçalho/data/grupo e faixas positivas. Usa a mediana dos preços
comuns por classificação, com fator de varejo padrão 1,6. Conversões usam
peso da embalagem ou contagem declarada no boletim; intervalos de contagem
usam o ponto médio. PROCON: só PDFs do domínio oficial, relatório de até
90 dias; atualmente mapeia apenas feijão carioquinha, com fator 1. Ambos são
referências regionais de São Paulo, não médias nacionais medidas.

Ao cadastrar, corrige a cotação desde a data observada por IPCA e grava
`source='estimated'`. O log `[grocery_baseline] Cadastrado` registra fonte,
data, cotações originais, unidade, quantidade, fator e valor efetivamente
gravado. As evidências iniciais e a lista sem fonte ficam em
`data/grocery_baseline/`. O CSV de evidência contém valores na data da fonte;
a importação simples desse CSV não aplica automaticamente correção desde
a data da cotação. Prefira o preenchimento automático implementado.

O catálogo original contém unidades/embalagens diferentes das descrições
do Android em alguns itens. Esta implementação preserva o preço de referência
da unidade comercial do seed e não calcula o custo da porção semanal. A
normalização de embalagens/quantidades no cliente exige trabalho separado;
não interpretar a soma como orçamento exato das embalagens exibidas.

Preços existentes são corrigidos pelo produto das taxas mensais, proporcional
ao tempo transcorrido em cada mês UTC desde `updated_at`. Assim, uma execução
semanal não aplica novamente um mês inteiro de inflação. Meses ainda não
publicados ou indisponíveis usam `IPCA_MONTHLY_FALLBACK` (padrão `0.003`,
fração mensal, não percentual). O arredondamento é em centavos a cada gravação.
O índice geral é uma aproximação, não uma cotação de cada alimento. Dados
publicados posteriormente não corrigem retroativamente o fallback já aplicado.

`source='manual'` permanece totalmente intacto, inclusive se uma edição manual
ocorrer durante o job. Itens sem preço-base continuam com `needs_manual=TRUE`.
A resposta do job mantém `{atualizados, sem_preco, erros}`; estimados fazem
parte de `atualizados`. Falhas do SIDRA são contabilizadas uma vez por execução.
O id do agendamento permanece `meli_grocery_price_weekly` por compatibilidade.
Rotas de leitura e migration 026 permanecem iguais.

## Importação inicial e preços fixos

`POST /api/admin/grocery-prices/import-csv`, com o Bearer SERVICE_TOKEN já
existente, aceita corpo CSV UTF-8 (inclusive BOM, separador vírgula ou ponto e
vírgula) ou JSON `[{"food_name":"nome canônico","price":12.34}]`.
O CSV tem cabeçalho `food_name,price`; preço decimal com vírgula precisa estar
entre aspas se o separador também for vírgula. Limites: 1 MiB e 5000 linhas.

- Sem parâmetro, ou `?mode=manual`: importa preços fixos protegidos do IPCA,
  com a mesma regra do PUT unitário. Um nome novo é inserido.
- `?mode=baseline`: importa com `source='estimated'`, habilitando as futuras
  correções por IPCA. Preserva registros já manuais, reportando-os em detalhes.

Valores precisam ser finitos, positivos, com até duas casas e no máximo
99999999.99. Linhas inválidas e nomes duplicados são reportados sem impedir
as demais. O conjunto válido é gravado em uma transação; falha de banco
reverte todo esse conjunto. Retorno: `{importados, erros, detalhes}`.
O log administrativo registra modo e contagens, sem corpo ou token.

O arquivo `grocery_baseline_template.csv` tem os 220 nomes e preços vazios.
Preencha com referências verificadas e unidade/embalagem compatível com o
catálogo. Não importe células vazias nem preços inventados.

## Verificação operacional

Inspeção somente de leitura em 06/10/2026: no RDS, os 220 registros estavam
com fonte `referencia_nacional`, todos com `price_avg=NULL` e
`needs_manual=TRUE`. Não havia histórico utilizável para estimar preços.
Essa fotografia não substitui uma consulta após o deploy/importação.

Depois do deploy pelo pipeline e do cadastro do baseline, disparar o POST
`/api/admin/grocery-prices/refresh` e conferir os logs `[price_cascade]`.

```sql
SELECT source, COUNT(*), COUNT(price_avg)
FROM grocery_price_cache GROUP BY source;
SELECT food_name FROM grocery_price_cache
WHERE needs_manual OR price_avg IS NULL ORDER BY food_name;
```

O teste com 220 históricos sintéticos comprova a cobertura do algoritmo na
indisponibilidade HTTP. Não comprova baseline nem cobertura real de produção.

Referências: https://sidra.ibge.gov.br/tabela/1737 e
https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/63/p/last%20120
