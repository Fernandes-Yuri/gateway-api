-- T-03: referências clínicas canônicas com rastreabilidade de fonte.
ALTER TABLE clinical_references ADD COLUMN IF NOT EXISTS institution VARCHAR(255);
ALTER TABLE clinical_references ADD COLUMN IF NOT EXISTS document_version VARCHAR(255);
ALTER TABLE clinical_references ADD COLUMN IF NOT EXISTS parameter VARCHAR(255);
ALTER TABLE clinical_references ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

INSERT INTO clinical_references
    (item_key, item_name, category, unit, min_target, max_target, min_critical,
     max_critical, interpretation_hint, institution, document_version, parameter, updated_at)
VALUES
    ('systolic_pressure', 'Pressão Sistólica', 'Cardiovascular', 'mmHg', 90, 129, 60, 180,
     'Faixa ótima < 120 mmHg. Normal: 120–129 mmHg. Pré-hipertensão: 130–139 mmHg.',
     'SBC (Sociedade Brasileira de Cardiologia) / OMS', 'Diretrizes Brasileiras de Hipertensão Arterial', 'Pressão Máxima Arterial', NOW()),
    ('diastolic_pressure', 'Pressão Diastólica', 'Cardiovascular', 'mmHg', 60, 84, 40, 110,
     'Faixa ótima < 80 mmHg. Normal: 80–84 mmHg. Pré-hipertensão: 85–89 mmHg.',
     'SBC (Sociedade Brasileira de Cardiologia) / OMS', 'Diretrizes Brasileiras de Hipertensão Arterial', 'Pressão Mínima Arterial', NOW()),
    ('heart_rate', 'Frequência Cardíaca em Repouso', 'Cardiovascular', 'bpm', 50, 100, 40, 130,
     'Batimentos por minuto em estado de repouso.', 'SBC / AHA Diretrizes', '2026', 'Ritmo Cardíaco', NOW()),
    ('glucose_fasting', 'Glicose em Jejum', 'Metabólico', 'mg/dL', 70, 99, 54, 300,
     'Jejum de 8 a 12 horas. Faixa de referência esperada: 70-99 mg/dL.',
     'SBD (Sociedade Brasileira de Diabetes) / ADA', '2026', 'Glicemia Basal', NOW()),
    ('glucose_postprandial', 'Glicose Pós-Prandial', 'Metabólico', 'mg/dL', 70, 140, 54, 300,
     'Medição realizada 1 a 2 horas após início da refeição (esperado < 140 mg/dL).',
     'SBD (Sociedade Brasileira de Diabetes) / ADA', '2026', 'Glicemia Pós-Refeição', NOW()),
    ('glucose_cgm', 'Glicose Sensor Contínuo (CGM)', 'Metabólico', 'mg/dL', 70, 180, 54, 300,
     'Tempo no Alvo (Time in Range - TIR): 70 a 180 mg/dL.',
     'SBD / Consenso Internacional CGM', '2026', 'Monitoramento Contínuo', NOW()),
    ('hba1c', 'Hemoglobina Glicada (HbA1c)', 'Metabólico', '%', 4, 5.6, 3.5, 10,
     'Média ponderada da glicemia dos últimos 90 a 120 dias.',
     'SBD (Sociedade Brasileira de Diabetes)', '2026', 'Fração de Hemoglobina Glicosilada', NOW()),
    ('imc', 'Índice de Massa Corporal (IMC)', 'Antropometria', 'kg/m²', 18.5, 24.9, 16, 35,
     'Razão peso / altura² para avaliação antropométrica populacional.',
     'OMS (Organização Mundial da Saúde)', '2026', 'Composição Corporal', NOW()),
    ('total_cholesterol', 'Colesterol Total', 'Perfil Lipídico', 'mg/dL', 120, 199, 80, 300,
     'Faixa desejável para a população geral adulta.', 'SBC / OMS', '2026', 'Lipídios Séricos', NOW()),
    ('triglycerides', 'Triglicerídeos', 'Perfil Lipídico', 'mg/dL', 50, 149, 30, 500,
     'Medição com jejum de 12 horas.', 'SBC / OMS', '2026', 'Triglicérides Séricos', NOW())
ON CONFLICT (item_key) DO UPDATE SET
    item_name = EXCLUDED.item_name,
    category = EXCLUDED.category,
    unit = EXCLUDED.unit,
    min_target = EXCLUDED.min_target,
    max_target = EXCLUDED.max_target,
    min_critical = EXCLUDED.min_critical,
    max_critical = EXCLUDED.max_critical,
    interpretation_hint = EXCLUDED.interpretation_hint,
    institution = EXCLUDED.institution,
    document_version = EXCLUDED.document_version,
    parameter = EXCLUDED.parameter,
    updated_at = EXCLUDED.updated_at;

INSERT INTO schema_migrations(version)
VALUES ('016_clinical_references_sources') ON CONFLICT DO NOTHING;
