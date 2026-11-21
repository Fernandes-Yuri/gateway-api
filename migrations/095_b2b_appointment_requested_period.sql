-- =============================================================
-- MIGRACAO 095 (B2B-33): periodo desejado do agendamento
--
-- Contexto: a UI B2B precisa separar a PREFERENCIA do participante
-- (`requestedPeriod`, ex.: "PROXIMA_SEMANA_MANHA") do horario ja
-- CONFIRMADO pelo parceiro (`scheduledAt`). Antes so existia o texto
-- livre `requested_slot_text`, sem um codigo estavel para badge.
--
-- Decisao de contrato:
--   - coluna NULLABLE e sem default: vinculos/agendamentos legados
--     ficam NULL e o cliente simplesmente nao mostra o chip;
--   - sem backfill e sem inferencia a partir de `requested_slot_text`
--     (o Rust apenas normaliza o valor enviado pelo cliente);
--   - `requested_slot_text` continua obrigatorio e intacto, portanto
--     clientes antigos (v1.9.6) seguem funcionando sem mudanca;
--   - valor normalizado em MAIUSCULAS pelo Rust (ate 40 chars).
--
-- Nao altera indices: o agendamento e sempre lido pela PK ou pelo
-- par (program_id, patient_user_id) ja indexado na 092.
--
-- Aditivo e idempotente: ADD COLUMN IF NOT EXISTS + constraint
-- condicional (DO block), podendo rodar de novo sem efeito extra.
-- =============================================================

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE program_appointments
    ADD COLUMN IF NOT EXISTS requested_period varchar(40);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'chk_program_appointments_requested_period'
           AND conrelid = 'program_appointments'::regclass
    ) THEN
        ALTER TABLE program_appointments
            ADD CONSTRAINT chk_program_appointments_requested_period
            CHECK (
                requested_period IS NULL
                OR (
                    requested_period = btrim(requested_period)
                    AND requested_period = upper(requested_period)
                    AND char_length(requested_period) BETWEEN 2 AND 40
                )
            );
    END IF;
END $$;
