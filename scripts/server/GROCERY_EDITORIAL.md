# Módulo editorial descontinuado

Packs por mercado, comparação de cestas e publicação semanal foram removidos do código
executável. Rotas antigas deixam de existir. As tabelas das migrações 059 e 060 permanecem
somente para preservar o histórico, sem rotas de leitura ou escrita.

O cadastro de referências usa `/api/admin/grocery/retail`, com `kind=quote`: embalagem,
mercado de origem, preço e data real da observação. O catálogo usa a referência mais
recente, sem minimizar preço por mercado. A loja serve apenas como fonte.

A migração 065 reaproveita pesquisas confirmadas sem condicionantes, inativa itens incomuns
sem foto e pratos compostos sem foto, e registra as inativações para rollback. Alimentos
básicos fora da lista explícita são preservados mesmo sem foto. Fotos do ingrediente,
alimento ou embalagem protegem a família do ingrediente. Famílias com IDs legados vinculados a alimentos de nome diferente também são protegidas,
para não retirar básicos renomeados. Não há exclusão em cascata.

Antes da aplicação, revise a lista explícita de 30 candidatos na migração. Quantidades
reais dependem dos registros ativos e imagens do banco. O rollback reativa somente os
registros marcados pela curadoria; pesquisas reaproveitadas permanecem como histórico.

Aplicar 058-065 antes da publicação coordenada de API, portal e Android. Um APK antigo
pode ainda tentar acessar as rotas removidas e deve ser atualizado. Listas locais não
são apagadas nem reescritas ao abrir o mercadinho.
