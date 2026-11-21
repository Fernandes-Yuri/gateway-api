-- =============================================================
-- Rollback da migracao 094 (versao de consentimento no vinculo).
--
-- O backfill e dado derivado de auditoria: nao ha estado anterior a
-- restaurar (a coluna nasceu NULL e nao havia valores previos), por
-- isso o rollback apenas remove a coluna e o invariante. O runtime
-- volta ao fallback de program_audit_logs automaticamente — nenhum
-- vinculo passa a ter detalhe clinico extra por causa do rollback,
-- porque a autorizacao continua exigindo o recibo com a versao
-- corrente e shared_categories atuais.
--
-- IMPORTANTE: reverta TAMBEM o binario do braga-edge neste rollback.
-- O resumo do responsavel passou a ler `e.consent_version`; com a
-- coluna removida e o binario novo no ar a consulta falha (500).
-- Ordem segura: parar o servico -> rollback -> deploy do binario
-- anterior -> subir o servico.
--
-- Idempotente: DROP ... IF EXISTS em ambos os passos.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE program_enrollments
    DROP CONSTRAINT IF EXISTS chk_program_enrollments_consent_version;

ALTER TABLE program_enrollments
    DROP COLUMN IF EXISTS consent_version;
