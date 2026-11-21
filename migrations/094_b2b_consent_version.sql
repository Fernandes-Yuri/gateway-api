-- =============================================================
-- MIGRACAO 094 (B2B-32): versao de consentimento vigente no vinculo
--
-- Contexto (regressao P1 da Sprint C): `clinical_detail_categories`
-- em rust/crates/braga-edge/src/b2b.rs so liberava detalhes clinicos
-- quando o ULTIMO recibo de consentimento em program_audit_logs tinha
-- metadata->>'consentVersion' = 'program-routine-v1'. Nao existia
-- coluna `consent_version` em program_enrollments e nao havia
-- backfill: todo vinculo anterior ao deploy perdia
-- `availableDetails` (medications/authorized_records) ate o
-- participante reconfirmar pelo app novo.
--
-- Esta migration:
--   1. adiciona program_enrollments.consent_version (text, NULL);
--   2. preenche apenas vinculos ACTIVE cujo ULTIMO recibo de
--      consentimento preenche TODAS as condicoes abaixo;
--   3. garante o invariante "versao nunca em branco".
--
-- Regra do backfill (espelha exatamente a consulta de fallback do
-- runtime, inclusive `created_at >= consented_at` para nao herdar
-- recibos de um ciclo de vinculo anterior):
--   - action IN ('ENROLLMENT_JOINED', 'CONSENT_UPDATED');
--   - actor_user_id = target_user_id = patient_user_id (decisao
--     tomada pelo proprio participante; linhas anonimizadas pela
--     LGPD ficam com NULL e sao ignoradas);
--   - esse ULTIMO recibo (nao um recibo antigo qualquer) tem
--     metadata->>'consentVersion' = 'program-routine-v1' (versao
--     corrente, ROUTINE_CONSENT_VERSION) — uma decisao mais recente
--     sem a versao corrente prevalece e bloqueia o backfill;
--   - latest_receipt.created_at >= enrollment.consented_at;
--   - sem ENROLLMENT_REVOKED/PARTICIPANT_REMOVED posterior a esse
--     recibo (id maior);
--   - enrollment.status = 'ACTIVE'.
--
-- Decisao conservadora: nada e inferido. Um recibo sem a versao
-- corrente (metadata com `consentVersion: null`, gerado por clientes
-- antigos) NAO autoriza o backfill. Linhas que ficarem NULL seguem
-- funcionando pelo fallback de auditoria do runtime, entao nao
-- preencher nao concede nem remove acesso por si so — apenas evita
-- depender do historico de auditoria em cada request.
--
-- Indice: nao ha indice novo. `program_enrollments` ja tem
-- UNIQUE (program_id, patient_user_id) e o par e sempre localizado
-- por essa chave; um indice parcial nas mesmas colunas seria
-- redundante e so pesaria no RDS Micro. O invariante fica no CHECK.
--
-- Aditivo e idempotente: ALTER ... IF NOT EXISTS e o UPDATE so toca
-- linhas com consent_version NULL, portanto rodar de novo nao causa
-- efeito extra. Nao altera tabelas/colunas consumidas pelo app
-- publicado (v1.9.6).
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. Versao de consentimento vigente no vinculo (NULL = legado/nao migrado)
ALTER TABLE program_enrollments
    ADD COLUMN IF NOT EXISTS consent_version text;

-- 2. Invariante: string vazia nao e versao valida (NULL continua permitido)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'chk_program_enrollments_consent_version'
           AND conrelid = 'program_enrollments'::regclass
    ) THEN
        ALTER TABLE program_enrollments
            ADD CONSTRAINT chk_program_enrollments_consent_version
            CHECK (consent_version IS NULL OR btrim(consent_version) <> '');
    END IF;
END $$;

-- 3. Backfill conservador dos vinculos ACTIVE com recibo corrente
--    `latest_receipt` elege o ULTIMO recibo de consentimento do
--    participante (qualquer versao) e e esse recibo que precisa
--    declarar a versao corrente; um recibo antigo com a versao
--    correta nao autoriza nada se a decisao mais recente a removeu.
WITH latest_receipt AS (
    SELECT DISTINCT ON (program_id, target_user_id)
           program_id,
           target_user_id,
           id AS audit_id,
           created_at,
           metadata->>'consentVersion' AS receipt_version
      FROM program_audit_logs
     WHERE action IN ('ENROLLMENT_JOINED', 'CONSENT_UPDATED')
       AND actor_user_id = target_user_id
     ORDER BY program_id, target_user_id, id DESC
), eligible AS (
    SELECT e.program_id,
           e.patient_user_id
      FROM program_enrollments e
      JOIN latest_receipt lr
        ON lr.program_id = e.program_id
       AND lr.target_user_id = e.patient_user_id
     WHERE e.status = 'ACTIVE'
       AND e.consent_version IS NULL
       AND lr.receipt_version = 'program-routine-v1'
       AND lr.created_at >= e.consented_at
       AND NOT EXISTS (
           SELECT 1
             FROM program_audit_logs revoked
            WHERE revoked.program_id = e.program_id
              AND revoked.target_user_id = e.patient_user_id
              AND revoked.action IN ('ENROLLMENT_REVOKED', 'PARTICIPANT_REMOVED')
              AND revoked.id > lr.audit_id
       )
)
UPDATE program_enrollments e
   SET consent_version = 'program-routine-v1',
       updated_at = NOW()
  FROM eligible
 WHERE e.program_id = eligible.program_id
   AND e.patient_user_id = eligible.patient_user_id;
