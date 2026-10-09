-- Organizador de exames exclusivamente local: expurgo definitivo sem migração de dados.
-- Sem CASCADE: uma dependência não prevista deve interromper a migration.
DROP TABLE IF EXISTS exam_cloud_consents;
DROP TABLE IF EXISTS exam_items;
DROP TABLE IF EXISTS exams;
DROP TABLE IF EXISTS clinical_references;
