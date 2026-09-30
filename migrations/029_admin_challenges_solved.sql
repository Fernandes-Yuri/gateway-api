-- Desafio antirrobo do portal: coluna de resolucao.
-- O portal agora valida a posicao no momento em que o operador SOLTA o
-- slider (endpoint /api/admin/challenge/check). Acerto com precisao >= 92%
-- marca a linha como resolvida; o submit final (/verify) passa a exigir a
-- marca em vez de comparar posicoes. Tentativa errada continua sendo
-- consumida pelo DELETE atomico e o cliente gera outro desafio.
ALTER TABLE admin_challenges ADD COLUMN IF NOT EXISTS solved BOOLEAN NOT NULL DEFAULT FALSE;
