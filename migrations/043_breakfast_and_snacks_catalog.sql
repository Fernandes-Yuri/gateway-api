-- Migração 043: Catálogo de Café da Manhã e Lanches da Tarde & Saneamento de Redundâncias.
-- Insere 20 novos alimentos essenciais e seus ingredientes de compra.
-- Desativa duplicatas cosméticas de preparo para busca limpa.
BEGIN;

SET LOCAL lock_timeout = '10s';

-- 1. Novos ingredientes no catálogo de compras (grocery_ingredients)
INSERT INTO grocery_ingredients (slug, name, unit, details, active)
VALUES
  ('pao-frances', 'Pão Francês Tradicional', 'un', jsonb_build_object('step', 1, 'minimum', 6, 'aliases', ARRAY['Pão Francês', 'Pão de Sal', 'Pão Francês de Padaria'], 'food_ids', ARRAY['food_221'], 'grams_per_unit', 50.0, 'canonical_group_slug', 'pao'), true),
  ('pao-de-forma-integral', 'Pão de Forma 100% Integral', 'pct', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Pão Integral', 'Pão de Forma'], 'food_ids', ARRAY['food_222'], 'grams_per_unit', 450.0, 'canonical_group_slug', 'pao'), true),
  ('goma-de-tapioca', 'Goma de Tapioca Hidratada', 'kg', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Massa de Tapioca', 'Tapioca'], 'food_ids', ARRAY['food_223'], 'canonical_group_slug', 'tapioca'), true),
  ('torrada-integral', 'Torrada Integral / Multigrãos', 'pct', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Torradas'], 'food_ids', ARRAY['food_227'], 'grams_per_unit', 140.0), true),
  ('pao-de-queijo', 'Pão de Queijo Tradicional', 'kg', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Pão de Queijo Mineiro'], 'food_ids', ARRAY['food_228']), true),
  ('milho-para-pipoca', 'Milho para Pipoca', 'pct', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Milho de Pipoca'], 'food_ids', ARRAY['food_229'], 'grams_per_unit', 500.0), true),
  ('biscoito-de-polvilho', 'Biscoito de Polvilho Assado', 'pct', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Biscoito de Polvilho'], 'food_ids', ARRAY['food_230'], 'grams_per_unit', 100.0), true),
  ('requeijao-cremoso', 'Requeijão Cremoso Tradicional', 'un', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Requeijão'], 'food_ids', ARRAY['food_233'], 'grams_per_unit', 200.0), true),
  ('manteiga-com-sal', 'Manteiga de Primeira Qualidade', 'pote', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Manteiga'], 'food_ids', ARRAY['food_234'], 'grams_per_unit', 200.0), true),
  ('queijo-mussarela', 'Queijo Mussarela Fatiado', 'kg', jsonb_build_object('step', 100, 'minimum', 200, 'aliases', ARRAY['Mussarela', 'Mozzarella'], 'food_ids', ARRAY['food_235']), true),
  ('atum-ralado-ao-natural', 'Atum Ralado ao Natural', 'lata', jsonb_build_object('step', 1, 'minimum', 2, 'aliases', ARRAY['Atum em Conserva', 'Atum'], 'food_ids', ARRAY['food_237'], 'grams_per_unit', 170.0), true),
  ('po-de-cafe-tradicional', 'Café Torrado e Moído Tradicional', 'pct', jsonb_build_object('step', 1, 'minimum', 1, 'aliases', ARRAY['Pó de Café', 'Café'], 'food_ids', ARRAY['food_238', 'food_239'], 'grams_per_unit', 500.0), true)
ON CONFLICT (slug) DO UPDATE
SET active = true,
    details = EXCLUDED.details;

-- 2. Novos alimentos em food_catalog
INSERT INTO food_catalog (remote_id, name, category, kcal, carbs, protein, fat, fiber, details, active)
VALUES
  ('food_221', 'Pão Francês Tradicional', 'Panificados & Pães', 300, 58.7, 8.0, 3.1, 2.3, jsonb_build_object(
    'remoteId', 'food_221', 'name', 'Pão Francês Tradicional', 'category', 'Panificados & Pães',
    'kcal', 300, 'carbsG', 58.7, 'proteinG', 8.0, 'fatG', 3.1, 'fiberG', 2.3,
    'isDiabetesSafe', false, 'isHypertensionSafe', true, 'status', 'MODERADO',
    'functionalTags', ARRAY['carboidrato_rapido', 'padaria_tradicional'],
    'healthBenefits', 'Fonte de energia rápida matinal tradicional brasileira.',
    'consumptionTip', 'Prefira consumir acompanhado de uma fonte de proteína como ovo ou queijo para reduzir a resposta glicêmica.',
    'servingSizeGrams', 50, 'servingUnit', '1 unidade (50g)', 'minServingGrams', 25, 'maxServingGrams', 100,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['pao-frances']
  ), true),

  ('food_222', 'Pão de Forma Integral', 'Panificados & Pães', 245, 44.0, 9.5, 3.5, 6.9, jsonb_build_object(
    'remoteId', 'food_222', 'name', 'Pão de Forma Integral', 'category', 'Panificados & Pães',
    'kcal', 245, 'carbsG', 44.0, 'proteinG', 9.5, 'fatG', 3.5, 'fiberG', 6.9,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['fibras_integrais', 'baixo_ig', 'saciedade'],
    'healthBenefits', 'Rico em fibras insolúveis que retardam a absorção do carboidrato e prolongam a saciedade.',
    'consumptionTip', 'Excelente base para torradas com ovos mexidos ou pasta de amendoim.',
    'servingSizeGrams', 50, 'servingUnit', '2 fatias (50g)', 'minServingGrams', 25, 'maxServingGrams', 100,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['pao-de-forma-integral']
  ), true),

  ('food_223', 'Goma de Tapioca Hidratada', 'Cereais & Fibras', 240, 60.0, 0.0, 0.0, 0.5, jsonb_build_object(
    'remoteId', 'food_223', 'name', 'Goma de Tapioca Hidratada', 'category', 'Cereais & Fibras',
    'kcal', 240, 'carbsG', 60.0, 'proteinG', 0.0, 'fatG', 0.0, 'fiberG', 0.5,
    'isDiabetesSafe', false, 'isHypertensionSafe', true, 'status', 'MODERADO',
    'functionalTags', ARRAY['sem_gluten', 'energia_rapida'],
    'healthBenefits', 'Opção natural livre de glúten e conservantes para o café da manhã.',
    'consumptionTip', 'Prepare na frigideira com recheio proteico (ovo, frango ou queijo) para equilibrar a carga glicêmica.',
    'servingSizeGrams', 60, 'servingUnit', '3 colheres de sopa cheias (60g)', 'minServingGrams', 30, 'maxServingGrams', 120,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['goma-de-tapioca']
  ), true),

  ('food_224', 'Crepioca Simples (Tapioca + Ovo)', 'Preparações Matinais', 175, 18.0, 9.0, 6.5, 0.5, jsonb_build_object(
    'remoteId', 'food_224', 'name', 'Crepioca Simples (Tapioca + Ovo)', 'category', 'Preparações Matinais',
    'kcal', 175, 'carbsG', 18.0, 'proteinG', 9.0, 'fatG', 6.5, 'fiberG', 0.5,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['proteina_alto_vb', 'saciedade', 'sem_gluten'],
    'healthBenefits', 'Combinação equilibrada de carboidrato e proteína que reduz o pico de insulina.',
    'consumptionTip', 'Bata 1 ovo com 2 colheres de tapioca e doure dos dois lados na frigideira antiaderente.',
    'servingSizeGrams', 90, 'servingUnit', '1 disco médio (90g)', 'minServingGrams', 50, 'maxServingGrams', 180,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['goma-de-tapioca', 'ovo-caipira']
  ), true),

  ('food_225', 'Panqueca de Aveia e Banana', 'Preparações Matinais', 165, 24.0, 7.5, 4.5, 3.2, jsonb_build_object(
    'remoteId', 'food_225', 'name', 'Panqueca de Aveia e Banana', 'category', 'Preparações Matinais',
    'kcal', 165, 'carbsG', 24.0, 'proteinG', 7.5, 'fatG', 4.5, 'fiberG', 3.2,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['beta_glucana', 'potassio', 'saciedade_prolongada'],
    'healthBenefits', 'Rica em fibras solúveis da aveia e potássio natural da banana para sustentação energética.',
    'consumptionTip', 'Amasse 1 banana com 1 ovo e 2 colheres de aveia em flocos e doure na frigideira.',
    'servingSizeGrams', 130, 'servingUnit', '1 unidade média (130g)', 'minServingGrams', 65, 'maxServingGrams', 260,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['banana-prata', 'ovo-caipira', 'aveia-em-flocos-finos']
  ), true),

  ('food_226', 'Mingau de Aveia com Leite', 'Preparações Matinais', 95, 14.0, 4.2, 2.4, 1.8, jsonb_build_object(
    'remoteId', 'food_226', 'name', 'Mingau de Aveia com Leite', 'category', 'Preparações Matinais',
    'kcal', 95, 'carbsG', 14.0, 'proteinG', 4.2, 'fatG', 2.4, 'fiberG', 1.8,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['conforto_termico', 'digestao_suave', 'beta_glucana'],
    'healthBenefits', 'Refeição morna e reconfortante de alta digestibilidade e proteção da mucosa gástrica.',
    'consumptionTip', 'Cozinhe 30g de aveia em 150ml de leite com canela em pó sem açúcar.',
    'servingSizeGrams', 200, 'servingUnit', '1 tigela média (200g)', 'minServingGrams', 100, 'maxServingGrams', 350,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['aveia-em-flocos-finos', 'leite-desnatado-pasteurizado']
  ), true),

  ('food_227', 'Torrada Tradicional / Integral', 'Panificados & Pães', 380, 72.0, 11.0, 5.0, 5.0, jsonb_build_object(
    'remoteId', 'food_227', 'name', 'Torrada Tradicional / Integral', 'category', 'Panificados & Pães',
    'kcal', 380, 'carbsG', 72.0, 'proteinG', 11.0, 'fatG', 5.0, 'fiberG', 5.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['crocancia', 'praticidade'],
    'healthBenefits', 'Opção prática e estável para consumo diário com pastas e queijos magros.',
    'consumptionTip', 'Acompanhe com queijo ricota, requeijão ou ovo mexido.',
    'servingSizeGrams', 30, 'servingUnit', '3 fatias (30g)', 'minServingGrams', 20, 'maxServingGrams', 60,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['torrada-integral']
  ), true),

  ('food_228', 'Pão de Queijo Tradicional', 'Panificados & Pães', 310, 38.0, 6.0, 15.0, 1.0, jsonb_build_object(
    'remoteId', 'food_228', 'name', 'Pão de Queijo Tradicional', 'category', 'Panificados & Pães',
    'kcal', 310, 'carbsG', 38.0, 'proteinG', 6.0, 'fatG', 15.0, 'fiberG', 1.0,
    'isDiabetesSafe', false, 'isHypertensionSafe', false, 'status', 'MODERADO',
    'functionalTags', ARRAY['afetivo_tradicional', 'sem_gluten'],
    'healthBenefits', 'Alimento tradicional afetivo rico em energia.',
    'consumptionTip', 'Consuma com moderação em dias de maior gasto calórico acompanhado de café preto.',
    'servingSizeGrams', 50, 'servingUnit', '2 unidades pequenas (50g)', 'minServingGrams', 25, 'maxServingGrams', 100,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['pao-de-queijo']
  ), true),

  ('food_229', 'Pipoca de Panela', 'Lanches & Snacks', 375, 65.0, 10.0, 9.0, 12.0, jsonb_build_object(
    'remoteId', 'food_229', 'name', 'Pipoca de Panela', 'category', 'Lanches & Snacks',
    'kcal', 375, 'carbsG', 65.0, 'proteinG', 10.0, 'fatG', 9.0, 'fiberG', 12.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['polifenois', 'alto_volume', 'fibras_puras'],
    'healthBenefits', 'Altíssima densidade de polifenóis antioxidantes e fibras alimentares com alto volume e baixa densidade calórica.',
    'consumptionTip', 'Estoure na panela com pouco óleo e tempere com ervas finas e pouco sal.',
    'servingSizeGrams', 30, 'servingUnit', '1 bacia pequena (30g)', 'minServingGrams', 15, 'maxServingGrams', 60,
    'suitableMeals', ARRAY['Lanche da Tarde'],
    'shoppingComponents', ARRAY['milho-para-pipoca']
  ), true),

  ('food_230', 'Biscoito de Polvilho Assado', 'Lanches & Snacks', 410, 75.0, 3.0, 11.0, 1.5, jsonb_build_object(
    'remoteId', 'food_230', 'name', 'Biscoito de Polvilho Assado', 'category', 'Lanches & Snacks',
    'kcal', 410, 'carbsG', 75.0, 'proteinG', 3.0, 'fatG', 11.0, 'fiberG', 1.5,
    'isDiabetesSafe', false, 'isHypertensionSafe', true, 'status', 'MODERADO',
    'functionalTags', ARRAY['leveza', 'sem_gluten', 'snack_pratico'],
    'healthBenefits', 'Digestão rápida e leve para intervalos vespertinos sem pesar no estômago.',
    'consumptionTip', 'Consuma uma porção controlada no lanche da tarde acompanhado de chá ou café.',
    'servingSizeGrams', 30, 'servingUnit', '1 porção individual (30g)', 'minServingGrams', 15, 'maxServingGrams', 50,
    'suitableMeals', ARRAY['Lanche da Tarde'],
    'shoppingComponents', ARRAY['biscoito-de-polvilho']
  ), true),

  ('food_231', 'Bolo de Fubá Caseiro Simples', 'Panificados & Pães', 310, 48.0, 5.5, 11.0, 2.0, jsonb_build_object(
    'remoteId', 'food_231', 'name', 'Bolo de Fubá Caseiro Simples', 'category', 'Panificados & Pães',
    'kcal', 310, 'carbsG', 48.0, 'proteinG', 5.5, 'fatG', 11.0, 'fiberG', 2.0,
    'isDiabetesSafe', false, 'isHypertensionSafe', true, 'status', 'MODERADO',
    'functionalTags', ARRAY['tradicao_brasileira', 'energia_conforto'],
    'healthBenefits', 'Opção tradicional de padaria e casa brasileira preparada com milho nacional.',
    'consumptionTip', 'Aprecie uma fatia fina no lanche da tarde com café coado sem açúcar.',
    'servingSizeGrams', 50, 'servingUnit', '1 fatia fina (50g)', 'minServingGrams', 30, 'maxServingGrams', 80,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['farinha-de-milho-em-flocos', 'ovo-caipira']
  ), true),

  ('food_232', 'Ovo Mexido Simples', 'Proteínas & Ovos', 155, 1.2, 13.0, 11.0, 0.0, jsonb_build_object(
    'remoteId', 'food_232', 'name', 'Ovo Mexido Simples', 'category', 'Proteínas & Ovos',
    'kcal', 155, 'carbsG', 1.2, 'proteinG', 13.0, 'fatG', 11.0, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['colina', 'proteina_completa', 'zero_carb'],
    'healthBenefits', 'Proteína de referência biológica máxima e colina para memória e preservação celular.',
    'consumptionTip', 'Prepare em fogo baixo com um fio de azeite e orégano.',
    'servingSizeGrams', 100, 'servingUnit', '2 ovos médios (100g)', 'minServingGrams', 50, 'maxServingGrams', 180,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde', 'Jantar'],
    'shoppingComponents', ARRAY['ovo-caipira']
  ), true),

  ('food_233', 'Requeijão Cremoso Tradicional', 'Laticínios & Derivados', 250, 3.5, 9.0, 23.0, 0.0, jsonb_build_object(
    'remoteId', 'food_233', 'name', 'Requeijão Cremoso Tradicional', 'category', 'Laticínios & Derivados',
    'kcal', 250, 'carbsG', 3.5, 'proteinG', 9.0, 'fatG', 23.0, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'MODERADO',
    'functionalTags', ARRAY['calcio', 'cremosidade', 'baixo_carb'],
    'healthBenefits', 'Cremosidade e sabor com cálcio lácteo para pães e cuscuz.',
    'consumptionTip', 'Espalhe 1 colher de sopa sobre torradas ou tapioca.',
    'servingSizeGrams', 30, 'servingUnit', '1 colher de sopa (30g)', 'minServingGrams', 15, 'maxServingGrams', 45,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['requeijao-cremoso']
  ), true),

  ('food_234', 'Manteiga com Sal', 'Gorduras & Manteigas', 720, 0.1, 0.8, 81.0, 0.0, jsonb_build_object(
    'remoteId', 'food_234', 'name', 'Manteiga com Sal', 'category', 'Gorduras & Manteigas',
    'kcal', 720, 'carbsG', 0.1, 'proteinG', 0.8, 'fatG', 81.0, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', false, 'status', 'MODERADO',
    'functionalTags', ARRAY['gordura_natural', 'butirato', 'zero_ultraprocessado'],
    'healthBenefits', 'Gordura natural sem óleos hidrogenados nem gorduras trans industriais.',
    'consumptionTip', 'Passe uma ponta de faca no cuscuz quente ou pão francês.',
    'servingSizeGrams', 10, 'servingUnit', '1 ponta de faca (10g)', 'minServingGrams', 5, 'maxServingGrams', 20,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['manteiga-com-sal']
  ), true),

  ('food_235', 'Queijo Mussarela em Fatias', 'Laticínios & Derivados', 285, 2.5, 22.0, 21.0, 0.0, jsonb_build_object(
    'remoteId', 'food_235', 'name', 'Queijo Mussarela em Fatias', 'category', 'Laticínios & Derivados',
    'kcal', 285, 'carbsG', 2.5, 'proteinG', 22.0, 'fatG', 21.0, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', false, 'status', 'MODERADO',
    'functionalTags', ARRAY['calcio', 'proteina_lactea', 'acessivel'],
    'healthBenefits', 'O queijo mais popular e acessível para complementar sanduíches e cuscuz com proteína.',
    'consumptionTip', 'Aqueça 1 fatia no pão integral ou na tapioca para derreter.',
    'servingSizeGrams', 30, 'servingUnit', '1 fatia média (30g)', 'minServingGrams', 15, 'maxServingGrams', 60,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['queijo-mussarela']
  ), true),

  ('food_236', 'Frango Desfiado Temperado', 'Carnes & Aves', 150, 0.5, 30.0, 3.2, 0.0, jsonb_build_object(
    'remoteId', 'food_236', 'name', 'Frango Desfiado Temperado', 'category', 'Carnes & Aves',
    'kcal', 150, 'carbsG', 0.5, 'proteinG', 30.0, 'fatG', 3.2, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['proteina_magra', 'saciedade_maxima', 'baixo_gordura'],
    'healthBenefits', 'Proteína magra desfiada e versátil para recheios nutritivos sem gordura saturada excessiva.',
    'consumptionTip', 'Use para rechear tapiocas, crepiocas ou sanduíches naturais.',
    'servingSizeGrams', 60, 'servingUnit', '2 colheres de sopa cheias (60g)', 'minServingGrams', 30, 'maxServingGrams', 120,
    'suitableMeals', ARRAY['Café da Manhã', 'Almoço', 'Lanche da Tarde', 'Jantar'],
    'shoppingComponents', ARRAY['peito-de-frango-sem-pele']
  ), true),

  ('food_237', 'Atum Ralado em Conserva', 'Peixes & Frutos do Mar', 120, 0.0, 26.0, 1.5, 0.0, jsonb_build_object(
    'remoteId', 'food_237', 'name', 'Atum Ralado em Conserva', 'category', 'Peixes & Frutos do Mar',
    'kcal', 120, 'carbsG', 0.0, 'proteinG', 26.0, 'fatG', 1.5, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['omega3', 'proteina_magra', 'praticidade'],
    'healthBenefits', 'Fonte de ômega-3 marinho e proteína rápida para lanches sem necessidade de cozimento.',
    'consumptionTip', 'Misture com ricota ou iogurte natural para criar um patê saudável.',
    'servingSizeGrams', 60, 'servingUnit', '1/2 lata (60g)', 'minServingGrams', 30, 'maxServingGrams', 120,
    'suitableMeals', ARRAY['Almoço', 'Lanche da Tarde', 'Jantar'],
    'shoppingComponents', ARRAY['atum-ralado-ao-natural']
  ), true),

  ('food_238', 'Café Preto Coado', 'Bebidas & Infusões', 1, 0.2, 0.1, 0.0, 0.0, jsonb_build_object(
    'remoteId', 'food_238', 'name', 'Café Preto Coado', 'category', 'Bebidas & Infusões',
    'kcal', 1, 'carbsG', 0.2, 'proteinG', 0.1, 'fatG', 0.0, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['cafeina', 'antioxidantes', 'polifenois', 'zero_calorias'],
    'healthBenefits', 'Rico em ácidos clorogênicos que estimulam a atenção, cognição e sensibilidade à insulina.',
    'consumptionTip', 'Aprecie puro ou com canela em pó, evitando adição de açúcar refinado.',
    'servingSizeGrams', 150, 'servingUnit', '1 xícara média (150ml)', 'minServingGrams', 50, 'maxServingGrams', 300,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['po-de-cafe-tradicional']
  ), true),

  ('food_239', 'Café com Leite (Pingado)', 'Bebidas & Infusões', 35, 3.5, 2.5, 1.5, 0.0, jsonb_build_object(
    'remoteId', 'food_239', 'name', 'Café com Leite (Pingado)', 'category', 'Bebidas & Infusões',
    'kcal', 35, 'carbsG', 3.5, 'proteinG', 2.5, 'fatG', 1.5, 'fiberG', 0.0,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['calcio', 'tradicao_matinal', 'energia_suave'],
    'healthBenefits', 'O clássico matinal brasileiro que combina o estímulo do café com o cálcio e proteína láctea.',
    'consumptionTip', 'Prefira preparar com leite desnatado ou integral sem açúcar.',
    'servingSizeGrams', 150, 'servingUnit', '1 xícara média (150ml)', 'minServingGrams', 100, 'maxServingGrams', 300,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['po-de-cafe-tradicional', 'leite-desnatado-pasteurizado']
  ), true),

  ('food_240', 'Vitamina de Banana com Leite e Aveia', 'Bebidas & Infusões', 78, 14.0, 3.2, 1.2, 1.2, jsonb_build_object(
    'remoteId', 'food_240', 'name', 'Vitamina de Banana com Leite e Aveia', 'category', 'Bebidas & Infusões',
    'kcal', 78, 'carbsG', 14.0, 'proteinG', 3.2, 'fatG', 1.2, 'fiberG', 1.2,
    'isDiabetesSafe', true, 'isHypertensionSafe', true, 'status', 'RECOMENDADO',
    'functionalTags', ARRAY['potassio', 'beta_glucana', 'refeicao_liquida'],
    'healthBenefits', 'Refeição líquida completa para manhãs corridas, rica em fibras solúveis, cálcio e potássio.',
    'consumptionTip', 'Bata 1 banana com 200ml de leite e 1 colher de sopa de aveia.',
    'servingSizeGrams', 250, 'servingUnit', '1 copo grande (250ml)', 'minServingGrams', 150, 'maxServingGrams', 350,
    'suitableMeals', ARRAY['Café da Manhã', 'Lanche da Tarde'],
    'shoppingComponents', ARRAY['banana-prata', 'leite-desnatado-pasteurizado', 'aveia-em-flocos-finos']
  ), true)
ON CONFLICT (remote_id) DO UPDATE
SET active = true,
    details = EXCLUDED.details,
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    kcal = EXCLUDED.kcal,
    carbs = EXCLUDED.carbs,
    protein = EXCLUDED.protein,
    fat = EXCLUDED.fat,
    fiber = EXCLUDED.fiber;

-- 3. Desativação de duplicatas/variações cosméticas no banco (mantendo histórico intacto)
UPDATE food_catalog
SET active = false
WHERE remote_id IN (
  'food_006', -- Linhaça Marrom Moída (já existe Linhaça Marrom Triturada food_002)
  'food_023', -- Amendoim Torrado sem Sal (duplicata de food_020)
  'food_063', -- Mamão Formosa em Cubos (já existe food_032)
  'food_064', -- Melancia em Fatias (já existe food_034)
  'food_065', -- Abacaxi Pérola em Fatias (já existe food_035)
  'food_066', -- Goiaba Branca em Fatias (já existe food_041)
  'food_068', -- Abacate Manteiga Nacional (já existe food_043)
  'food_069', -- Mamão Papaia Maduro (já existe food_031)
  'food_155', -- Feijão Preto Cozido Simples (já existe food_150)
  'food_163', -- Arroz Integral Agulhinha Cozido (já existe food_162)
  'food_176', -- Peito de Frango em Cubos Grelhado (já existe food_175)
  'food_193', -- Leite Desnatado Fortificado (já existe food_192)
  'food_196'  -- Ovo Poché Simples (já existe food_195)
);

-- 4. Incremento da revisão do catálogo para forçar sincronização fresca no app
UPDATE nutrition_catalog_settings
SET revision = revision + 1
WHERE id = 1;

COMMIT;
