# Preços de alimentos por IPCA

O job de segunda-feira às 03:00 America/Sao_Paulo usa a API pública SIDRA,
tabela 1737, variável 63. Não consulta MELI e não exige nova chave ou pacote.
CONAB/CEAGESP/PROCON/DIEESE ficam para uma segunda fase: não há feed CSV
estável verificado para conectar nesta implementação.

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
