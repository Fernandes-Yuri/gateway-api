# Referência nutricional TACO

Fonte: NEPA/UNICAMP, Tabela Brasileira de Composição de Alimentos, 4ª edição revisada e ampliada, Campinas, 2011.
Planilha oficial e publicação: https://nepa.unicamp.br/publicacoes/
A publicação permite reprodução total ou parcial mediante citação da fonte.

O JSON é gerado da planilha oficial por `scripts/build_taco_reference.py`; seu manifesto registra origem e SHA-256. O arquivo distribuído contém 597 alimentos. As abas da planilha oficial mantêm os nomes legados “taco3”. Não se trata de uma edição nova ou de um banco de rótulos de marcas.

Valores referem-se a 100 g da parte comestível. A aplicação distingue números, traços (Tr), não aplicável (NA) e ausência de informação; não converte os três últimos em zero. Gorduras saturadas, monoinsaturadas e poli-insaturadas vêm da aba de ácidos graxos da mesma planilha. Não misturar valores de outras bases em uma referência.

Para óleo de soja (TACO:272), adota-se 0,92 g/ml como aproximação operacional da densidade, com base na faixa de densidade relativa 0,919–0,925 a 20 °C da referência Codex/FAO. A aproximação e a fonte aparecem na prévia, separadas da composição TACO:
https://www.fao.org/4/Y2774E/y2774e04.htm

Não existe hipótese geral de 1 ml = 1 g. Para outros líquidos e embalagens em unidades sem massa conhecida, a prévia solicita o peso comestível total em gramas ou permite manter o preenchimento manual. Todo preenchimento exige seleção explícita e conferência antes de usar/salvar; não publica alimentos automaticamente.
