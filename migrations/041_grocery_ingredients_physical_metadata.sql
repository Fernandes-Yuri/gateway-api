-- Metadados opcionais, compatíveis com clientes anteriores. Nenhum alimento novo.
BEGIN;

CREATE OR REPLACE FUNCTION grocery_physical_metadata_valid(value JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE field TEXT;
BEGIN
    IF value IS NULL THEN RETURN TRUE; END IF;
    FOREACH field IN ARRAY ARRAY['grams_per_unit','density_g_per_ml'] LOOP
        IF value ? field AND value->field <> 'null'::jsonb THEN
            IF jsonb_typeof(value->field) <> 'number' THEN RETURN FALSE; END IF;
            IF (value->>field)::numeric <= 0 THEN RETURN FALSE; END IF;
        END IF;
    END LOOP;
    IF value ? 'canonical_group_slug' AND value->'canonical_group_slug' <> 'null'::jsonb THEN
        IF jsonb_typeof(value->'canonical_group_slug') <> 'string' OR btrim(value->>'canonical_group_slug') = '' THEN RETURN FALSE; END IF;
    END IF;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION grocery_component_proportions_valid(value JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE proportion RECORD; total NUMERIC := 0; refs JSONB;
BEGIN
    IF value IS NULL OR NOT value ? 'component_proportions' OR value->'component_proportions' = 'null'::jsonb THEN RETURN TRUE; END IF;
    IF jsonb_typeof(value->'component_proportions') <> 'object' THEN RETURN FALSE; END IF;
    refs := value->'shoppingComponents';
    IF refs IS NULL OR jsonb_typeof(refs) <> 'array' THEN RETURN FALSE; END IF;
    IF jsonb_array_length(refs) <> (SELECT count(*) FROM jsonb_object_keys(value->'component_proportions')) THEN RETURN FALSE; END IF;
    FOR proportion IN SELECT * FROM jsonb_each(value->'component_proportions') LOOP
        IF NOT refs ? proportion.key OR jsonb_typeof(proportion.value) <> 'number' THEN RETURN FALSE; END IF;
        IF proportion.value::text::numeric <= 0 THEN RETURN FALSE; END IF;
        total := total + proportion.value::text::numeric;
    END LOOP;
    RETURN abs(total - 1) < 0.000001;
END $$;

-- PostgreSQL não oferece jsonb_object_length: contar as chaves sem depender de versão.

ALTER TABLE grocery_ingredients DROP CONSTRAINT IF EXISTS grocery_physical_metadata_check;
ALTER TABLE grocery_ingredients ADD CONSTRAINT grocery_physical_metadata_check CHECK (grocery_physical_metadata_valid(details));
ALTER TABLE food_catalog DROP CONSTRAINT IF EXISTS grocery_component_proportions_check;
ALTER TABLE food_catalog ADD CONSTRAINT grocery_component_proportions_check CHECK (grocery_component_proportions_valid(details));

-- Referências aproximadas fornecidas na solicitação: não são medições de cada produto.
-- Preservar valores já cadastrados; densidades não são aplicadas a bebidas sem referência.
UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('grams_per_unit', 50.0,
    'physical_metadata_source', 'referência de planejamento: unidade comestível estimada; varia por tamanho')
WHERE slug IN ('ovo-caipira', 'ovos') AND (details->>'grams_per_unit') IS NULL;
UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('density_g_per_ml', 0.92,
    'physical_metadata_source', 'referência aproximada de planejamento; varia com produto e temperatura')
WHERE slug IN ('azeite-de-oliva-extravirgem', 'oleo-de-abacate-extravirgem', 'oleo-de-soja') AND (details->>'density_g_per_ml') IS NULL;
UPDATE grocery_ingredients
SET details = coalesce(details, '{}'::jsonb) || jsonb_build_object('density_g_per_ml', 1.03,
    'physical_metadata_source', 'referência aproximada de planejamento; varia com produto e temperatura')
WHERE slug = 'leite-desnatado-pasteurizado' AND (details->>'density_g_per_ml') IS NULL;

-- Não inventar fichas técnicas de receitas, nem agrupar cortes sem política definida.
UPDATE nutrition_catalog_settings SET revision = greatest(revision, 3) WHERE id = 1;
COMMIT;
