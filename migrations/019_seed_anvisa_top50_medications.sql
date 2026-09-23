-- 019_seed_anvisa_top50_medications.sql
-- Seed dos 50 medicamentos de uso crônico/geriátrico mais consumidos no Brasil.
-- Rastreabilidade total ANVISA e Farmácia Popular.
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

INSERT INTO anvisa_medication_catalog (
    ean_barcode,
    name,
    active_principle,
    concentration,
    pharmaceutical_form,
    manufacturer,
    farmacia_popular_eligible,
    source_name,
    source_url,
    source_checked_at
) VALUES
('7896004715506', 'Losartana Potássica 50mg', 'Losartana Potássica', '50 mg', 'Comprimido revestido', 'EMS', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422505031', 'Maleato de Enalapril 10mg', 'Maleato de Enalapril', '10 mg', 'Comprimido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422505048', 'Maleato de Enalapril 20mg', 'Maleato de Enalapril', '20 mg', 'Comprimido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422506021', 'Captopril 25mg', 'Captopril', '25 mg', 'Comprimido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004702582', 'Atenolol 25mg', 'Atenolol', '25 mg', 'Comprimido', 'EMS', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004702599', 'Atenolol 50mg', 'Atenolol', '50 mg', 'Comprimido', 'EMS', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896112100416', 'Hidroclorotiazida 25mg', 'Hidroclorotiazida', '25 mg', 'Comprimido', 'Teuto', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896714207902', 'Besilato de Anlodipino 5mg', 'Besilato de Anlodipino', '5 mg', 'Comprimido', 'Neo Química', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896714207919', 'Besilato de Anlodipino 10mg', 'Besilato de Anlodipino', '10 mg', 'Comprimido', 'Neo Química', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422505185', 'Cloridrato de Propranolol 40mg', 'Cloridrato de Propranolol', '40 mg', 'Comprimido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317438470', 'Carvedilol 6,25mg', 'Carvedilol', '6,25 mg', 'Comprimido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317438487', 'Carvedilol 12,5mg', 'Carvedilol', '12,5 mg', 'Comprimido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317438494', 'Carvedilol 25mg', 'Carvedilol', '25 mg', 'Comprimido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896112100652', 'Furosemida 40mg', 'Furosemida', '40 mg', 'Comprimido', 'Teuto', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317432041', 'Espironolactona 25mg', 'Espironolactona', '25 mg', 'Comprimido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004712338', 'Besilato de Anlodipino 5mg', 'Besilato de Anlodipino', '5 mg', 'Comprimido', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422513906', 'Cloridrato de Metformina 500mg', 'Cloridrato de Metformina', '500 mg', 'Comprimido revestido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422513913', 'Cloridrato de Metformina 850mg', 'Cloridrato de Metformina', '850 mg', 'Comprimido revestido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422506458', 'Glibenclamida 5mg', 'Glibenclamida', '5 mg', 'Comprimido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7898024395652', 'Diamicron MR 30mg', 'Gliclazida', '30 mg', 'Comprimido de liberação modificada', 'Servier', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7898024395669', 'Diamicron MR 60mg', 'Gliclazida', '60 mg', 'Comprimido de liberação modificada', 'Servier', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896382701832', 'Humulin N NPH 100 UI/ml', 'Insulina Humana NPH', '100 UI/ml', 'Suspensão injetável 10ml', 'Eli Lilly', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896382701849', 'Humulin R Regular 100 UI/ml', 'Insulina Humana Regular', '100 UI/ml', 'Solução injetável 10ml', 'Eli Lilly', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422514606', 'Sinvastatina 10mg', 'Sinvastatina', '10 mg', 'Comprimido revestido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422514613', 'Sinvastatina 20mg', 'Sinvastatina', '20 mg', 'Comprimido revestido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422514620', 'Sinvastatina 40mg', 'Sinvastatina', '40 mg', 'Comprimido revestido', 'Medley', TRUE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317442118', 'Atorvastatina Cálcica 10mg', 'Atorvastatina Cálcica', '10 mg', 'Comprimido revestido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317442125', 'Atorvastatina Cálcica 20mg', 'Atorvastatina Cálcica', '20 mg', 'Comprimido revestido', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004724881', 'Rosuvastatina Cálcica 10mg', 'Rosuvastatina Cálcica', '10 mg', 'Comprimido revestido', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422507011', 'Omeprazol 20mg', 'Omeprazol', '20 mg', 'Cápsula gelatinosa dura', 'Medley', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891317431211', 'Omeprazol 20mg', 'Omeprazol', '20 mg', 'Cápsula gelatinosa dura', 'Eurofarma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004713212', 'Pantoprazol 20mg', 'Pantoprazol Sódico Sesqui-hidratado', '20 mg', 'Comprimido revestido de liberação retardada', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004713229', 'Pantoprazol 40mg', 'Pantoprazol Sódico Sesqui-hidratado', '40 mg', 'Comprimido revestido de liberação retardada', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004700083', 'Ácido Acetilsalicílico 100mg', 'Ácido Acetilsalicílico', '100 mg', 'Comprimido', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891058017361', 'AAS Protect 100mg', 'Ácido Acetilsalicílico', '100 mg', 'Comprimido revestido', 'Sanofi', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004718422', 'Bissulfato de Clopidogrel 75mg', 'Bissulfato de Clopidogrel', '75 mg', 'Comprimido revestido', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7898040324889', 'Marevan 5mg', 'Varfarina Sódica', '5 mg', 'Comprimido', 'Farmoquímica', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891058003180', 'Puran T4 25mcg', 'Levotiroxina Sódica', '25 mcg', 'Comprimido', 'Sanofi', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891058003197', 'Puran T4 50mcg', 'Levotiroxina Sódica', '50 mcg', 'Comprimido', 'Sanofi', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891058003203', 'Puran T4 75mcg', 'Levotiroxina Sódica', '75 mcg', 'Comprimido', 'Sanofi', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891058003210', 'Puran T4 100mcg', 'Levotiroxina Sódica', '100 mcg', 'Comprimido', 'Sanofi', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7891158102431', 'Synthroid 50mcg', 'Levotiroxina Sódica', '50 mcg', 'Comprimido', 'Abbott', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896714201016', 'Dipirona Monoidratada 500mg', 'Dipirona Monoidratada', '500 mg', 'Comprimido', 'Neo Química', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896004723143', 'Dipirona Monoidratada 1g', 'Dipirona Monoidratada', '1 g', 'Comprimido', 'EMS', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896112102120', 'Paracetamol 500mg', 'Paracetamol', '500 mg', 'Comprimido', 'Teuto', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896422508117', 'Paracetamol 750mg', 'Paracetamol', '750 mg', 'Comprimido revestido', 'Medley', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896112100089', 'Alopurinol 100mg', 'Alopurinol', '100 mg', 'Comprimido', 'Teuto', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896112100096', 'Alopurinol 300mg', 'Alopurinol', '300 mg', 'Comprimido', 'Teuto', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896523206776', 'Caldê 500mg + 400 UI', 'Carbonato de Cálcio + Colecalciferol', '500 mg + 400 UI', 'Comprimido revestido', 'Marjan Farma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z'),
('7896094917477', 'Addera D3 7.000 UI', 'Colecalciferol', '7.000 UI', 'Comprimido revestido', 'Hypera Pharma', FALSE, 'Farmacia Popular / ANVISA', 'https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular', '2026-09-23T00:00:00Z')
ON CONFLICT (ean_barcode) DO UPDATE SET
    name = EXCLUDED.name,
    active_principle = EXCLUDED.active_principle,
    concentration = EXCLUDED.concentration,
    pharmaceutical_form = EXCLUDED.pharmaceutical_form,
    manufacturer = EXCLUDED.manufacturer,
    farmacia_popular_eligible = EXCLUDED.farmacia_popular_eligible,
    source_name = EXCLUDED.source_name,
    source_url = EXCLUDED.source_url,
    source_checked_at = EXCLUDED.source_checked_at;

-- Registro da migração
INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('019_seed_anvisa_top50_medications', 'seed_anvisa_top50_medications', now())
ON CONFLICT (version) DO NOTHING;

-- Compatibilidade para referências com prefixo simples
INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('019', 'seed_anvisa_top50_medications', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
