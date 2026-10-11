-- Migração 049: extinção da lista de espera VIP.
-- A lista VIP existia para distribuir convites do app antes do lançamento.
-- Ela não tem mais interface nem consumo: o formulário saiu do site, as rotas
-- /api/public/vip-signups saíram do gateway e a lista histórica nunca migrou
-- para o PostgreSQL (ficou no Firestore, com export_vip_firestore.py legado).
-- A tabela em produção tem apenas registros de teste, então o DROP não perde
-- dado de usuário.
BEGIN;

SET LOCAL lock_timeout = '10s';

DROP TABLE IF EXISTS vip_signups;

COMMIT;
