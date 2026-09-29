-- Nome público separado do perfil clínico; o upsert legado não altera esta coluna.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS community_nickname varchar(24);
