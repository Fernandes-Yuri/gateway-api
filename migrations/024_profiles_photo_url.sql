-- 024_profiles_photo_url.sql
-- ============================================================
-- Foto de perfil (avatar) do PWA Cuidador.
--
-- A coluna propositalmente FORA do upsert de perfil
-- (PROFILE_FIELDS/profile_upsert): o app Android reenvia o perfil
-- inteiro a cada sync e o PWA usa o mesmo endpoint com posts
-- parciais; se photo_url estivesse nesse caminho, um sync sem a
-- foto apagaria a imagem do outro cliente (e vice-versa).
-- A foto so muda pelos endpoints dedicados:
--   POST   /api/profile/{id}/avatar  (sobe nova foto)
--   DELETE /api/profile/{id}/avatar  (remove e volta as iniciais)
-- Leitura: GET /api/profile/{id} ja devolve a linha inteira e o
-- SELECT do feed social inclui p.photo_url para autores do mural.
-- ============================================================
BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS photo_url varchar(512);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES ('024_profiles_photo_url', 'profiles_photo_url', now())
ON CONFLICT (version) DO NOTHING;

COMMIT;
