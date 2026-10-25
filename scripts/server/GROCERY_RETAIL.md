# Catálogo de compras e referências

O app é um simulador de orçamento alimentar com navegação visual e seleção livre.
Não vende produtos nem garante ofertas de terceiros ou disponibilidade.

- `/api/catalog/grocery/cards`: embalagens ativas, com preço da observação mais recente e sua fonte; sem pesquisa, mantém a estimativa cadastrada.
- `/api/admin/grocery/retail`: gestão de alimentos, embalagens, mercados e pesquisas.
- `quote`: preço observado por embalagem, mercado de origem e data real da pesquisa.
- Imagens de alimentos são opcionais; nomes básicos permanecem disponíveis sem foto.
- Comparação de cestas, rankings, packs e publicação semanal foram removidos.

Para a curadoria reversível e a compatibilidade com históricos, veja [GROCERY_EDITORIAL.md](GROCERY_EDITORIAL.md).
