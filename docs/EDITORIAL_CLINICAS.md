# Publicação de notícias e artigos das clínicas

A gateway é a autoridade de autorização, validação, persistência e publicação. O painel Next.js do webservice consome `/api/admin/editorial/*` com a sessão já existente; sua ponte servidor-servidor usa a identidade assinada vinculada à rota. Nenhuma credencial de serviço chega ao browser. O site público consome apenas `/api/editorial/*`.

## Cadastro e acesso

O cadastro do portal exige nome, e-mail, senha, desafio antirrobô e código TOTP mestre. `admin_register` valida o código na gateway com o mecanismo já existente de limite compartilhado e uso único e cria a conta e a permissão `publisher` na mesma transação. Qualquer e-mail pode ser usado, mas nenhum cadastro é autorizado sem o TOTP. A configuração existente `ADMIN_TOTP_SECRET` continua exclusivamente no backend: não é regenerada nem exposta por esta implementação.

Contas preexistentes não recebem autorização editorial implicitamente. `superadmin` gerencia acessos; uma conta `admin` precisa de entrada ativa em `editorial_members`. `writer` lê/edita os próprios rascunhos, `publisher` revisa/publica/retira qualquer artigo. A revogação é consultada no banco a cada operação. Não há botão público de geração ou recuperação da chave mestre. O TOTP pessoal da conta, disponível no portal, é separado do TOTP mestre usado para autorizar cadastro.

## Publicação

Migration `067_clinicas_editorial.sql`, isolada dos registros clínicos. Rascunho JSON e versão publicada são separados; salvar não modifica a versão pública. Conflitos de versão retornam 409. Slug fica fixo após a primeira publicação; o endereço `politica-editorial` é reservado. Publicar exige confirmação editorial e fontes; capa exige descrição e crédito. Conteúdo admite parágrafos, subtítulos `##`/`###` e listas `-`, sem HTML executável. Campos de autoria e revisão técnica são públicos e devem refletir pessoas/credenciais verificadas.

Cada publicação/retirada registra snapshot e ator em `editorial_events`; salvamentos e mudanças de acesso também são registrados. A API pública projeta apenas o snapshot publicado, sem e-mails de acesso, rascunhos ou eventos. Nenhum artigo é publicado como exemplo ou inventado pelo deploy.

Capa pode ser URL HTTPS ou arquivo PNG/JPEG/WebP até 2 MB. Arquivos ficam em `editorial_media`, vinculados a um artigo e sujeitos à mesma autorização. A rota pública só serve a capa referenciada pela versão publicada de artigo no ar. Não aceita SVG/HTML. Uploads não usados ficam privados; planejar uma rotina de retenção ao escalar o acervo.

## SEO e entrega

O Firebase Hosting das clínicas reescreve `/noticias`, `/noticias/**` e `/sitemap.xml` para a função de apresentação `clinicasEditorial`. Ela consome a API pública com timeout, sem credenciais e sem acesso ao banco, e produz HTML completo com canonical, autoria, datas, fontes, Open Graph e Article/NewsArticle. Negócio permanece na gateway. Cache público dura até 60s; falha de origem retorna 503/noindex, artigo inexistente retorna 404/noindex. Política editorial é uma página própria.

Ordem de rollout: gateway/migration, portal, função de apresentação, Hosting. Verificar que a borda/WAF permite GET anônimo dos caminhos públicos editoriais, inclusive a partir do runtime Firebase. Não expor rotas administrativas nem usar bypass administrativo no renderer público.

## Verificações

`PYTHONPATH=scripts/server python -m unittest discover scripts/server/ 'test_*.py'` inclui autorização e testes de contrato. `test_editorial_postgres.py` usa apenas `TEST_POSTGRES_URL`, schema descartável e conteúdo sintético para exercitar migration idempotente, rascunho, publicação, conflito e retirada. O CI já fornece PostgreSQL efêmero. Nunca apontar esse teste para produção.
