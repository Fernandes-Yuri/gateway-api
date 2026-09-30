# Autenticação e preços do portal

O gateway deve ser publicado antes do portal correspondente. A migração
`028_admin_auth_security.sql` adiciona `session_version` e tabelas de limite,
consumo de TOTP mestre e desafios. Ela é aditiva e deve rodar pelo pipeline
normal. `SERVICE_TOKEN` permanece o canal de serviço; `ADMIN_TOTP_SECRET`
continua sendo o segredo mestre já usado no cadastro.

As rotas de TOTP e de verificação de sessão recebem `X-Admin-Identity`:
payload Base64url com e-mail, caminho e expiração, seguido de HMAC-SHA256
hexadecimal usando `SERVICE_TOKEN`. O portal emite a declaração só após
validar a assinatura da sessão. O gateway exige assinatura, prazo de até
60 segundos e escopo da rota. O token de sessão nunca é confundido com
o Bearer de serviço.

`POST /api/admin/password-reset` exige código mestre, limita tentativas
globalmente e consome a janela TOTP uma única vez entre cadastro e recuperação.
A recuperação preserva o 2FA pessoal, grava bcrypt e incrementa a versão
da sessão na mesma transação do consumo. E-mails inexistentes recebem a
mesma resposta de sucesso após autorização válida.

O portal consulta `POST /api/admin/session-check` em cada operação
autenticada. Tokens anteriores à recuperação são recusados. Não publicar
esta funcionalidade mantendo uma versão antiga do portal que desconhece
revogação. O login alternativo por credenciais de ambiente foi retirado
do novo portal: confirmar que os operadores existem em `admin_users`.

`/api/admin/challenge/create` e `/verify` são exclusivos do serviço. O
portal gera os pixels e o cookie HttpOnly; o gateway guarda a posição
esperada, o hash de vínculo e a finalidade. Um DELETE com RETURNING
consome cada tentativa atomicamente. Validade: dois minutos. A verificação
visual é uma camada contra automação simples, complementada por limites
persistentes por conta, vínculo e operação; não representa detecção de
fraude equivalente à de uma exchange.

A confirmação de TOTP ocorre no gateway com bloqueio da linha, impedindo
dupla ativação ou substituição de um vínculo já ativo. Os códigos de
recuperação são hashes bcrypt em `TEXT[]`; consumo concorrente usa
comparação e troca para que o mesmo código não autentique duas vezes.

O ajuste manual de qualquer item de preço continua substituindo média,
mínimo e máximo. Valores não finitos, fora de NUMERIC(10,2) ou com fração
de centavo são rejeitados. Coletas MELI preservam preços manuais e sua
data de alteração.

Testes isolados: `cd scripts/server` e
`python -m unittest test_portal_auth_security -v`. Os testes simulam o
banco. A migração e os fluxos com PostgreSQL real ainda precisam de
validação no ambiente de homologação antes da publicação em produção.
