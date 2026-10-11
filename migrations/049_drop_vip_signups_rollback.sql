-- Rollback da migração 049: recria a tabela da lista VIP vazia.
-- O DROP não é reversível em dados: as linhas da lista (se alguma existia)
-- não voltam. Este rollback só restaura o schema para quem precisar
-- reimplementar o formulário de espera.
BEGIN;

SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS vip_signups (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL CHECK (length(btrim(full_name)) BETWEEN 1 AND 255),
    email text NOT NULL UNIQUE CHECK (email = lower(btrim(email)) AND length(email) BETWEEN 3 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'PENDING',
    source text NOT NULL DEFAULT 'onboarding-site'
);

COMMIT;
