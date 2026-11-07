-- Correção operacional autorizada no plano de integridade do cuidador.
-- Executar via canal administrativo com psql -v ON_ERROR_STOP=1 -f este-arquivo.
-- Não é migration automática; histórico clínico e vínculos são preservados.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
BEGIN
    PERFORM 1 FROM profiles WHERE id = 'rA7h0gcYeJh79n21L4PRJKgrYrl2' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Perfil alvo não encontrado; nenhuma alteração aplicada';
    END IF;
    UPDATE profiles
    SET user_role = 'PATIENT', caregiver_mode = NULL,
        self_care_setup_pending = false, updated_at = NOW()
    WHERE id = 'rA7h0gcYeJh79n21L4PRJKgrYrl2';
END $$;
SELECT id, user_role, caregiver_mode FROM profiles
WHERE id = 'rA7h0gcYeJh79n21L4PRJKgrYrl2';
COMMIT;
