-- =============================================================
-- MIGRACAO 053: biometria do app no backend (PR2, passo 2.4).
--
-- Contrato do app (RemoteBiometry): user_id, weight, height, imc,
-- measured_at. Até aqui a biometria era só local (Room) e o WhatsApp gravava
-- peso em vital_signs.weight_kg — divergência: o peso do WhatsApp nunca
-- aparecia na biometria do app e vice-versa.
--
-- Agora o peso confirmado no WhatsApp é gravado aqui (mesmo destino lógico
-- do app), com a altura do perfil quando existir. IMC é calculado só com
-- altura real; altura nunca é inventada.
--
-- Reversivel: DROP TABLE.
-- =============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS biometrics (
    id                UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id           VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    weight_kg         DOUBLE PRECISION NOT NULL,
    height_m          DOUBLE PRECISION,
    imc               DOUBLE PRECISION,
    measured_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source            VARCHAR(20)  NOT NULL DEFAULT 'app',
    confirmation_code VARCHAR(20),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_biometrics_user_measured
    ON biometrics (user_id, measured_at DESC);

INSERT INTO schema_migrations(version) VALUES ('053_biometrics') ON CONFLICT DO NOTHING;
COMMIT;
