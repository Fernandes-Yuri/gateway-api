# Encerramento de alimentação

A aplicação não lê nem grava mais no módulo. As rotas legadas retornam HTTP 410 sem acessar PostgreSQL. Catálogo e preços não recebem seeds ou jobs.

A etapa destrutiva foi autorizada pelo usuário em 26/10/2026. A migração definitiva é `075_retire_nutrition.sql`; será aplicada pelo runner transacional no deploy após confirmação de disponibilidade do snapshot. O SQL manual mantém seu gate para execuções avulsas e ensaios.

Inventário online revisado: banco `bragasaude`, RDS `braga-saude-database`, região `us-east-2`; 20 tabelas exclusivas com 1823 linhas, nenhuma FK externa ao módulo. Snapshot criptografado solicitado: `braga-before-nutrition-retirement-20261026-190311`, com revisão de retenção em 02/11/2026.

Procedimento operacional:

1. Publicar o backend e os clientes sem alimentação; conferir que erro 410 de catálogo não interrompe saúde/sincronização em clientes antigos, ou exigir atualização.
2. Executar `nutrition_inventory.sql` no ambiente alvo e revisar dependências não previstas, contagens e armazenamento externo de imagens/exports. O inventário não imprime registros pessoais.
3. Criar backup recuperável, registrar identificador, retenção e evidência de restauração. Não incluir dados ou credenciais no PR.
4. Apresentar SQL, ambiente, contagens e backup para confirmação da exclusão.
5. Na mesma sessão, definir `SET braga.allow_nutrition_purge = 'on'` e executar `retire_nutrition.sql` com parada em erro. A transação usa RESTRICT implícito; dependência inesperada aborta tudo, sem CASCADE.
6. Reexecutar o inventário, conferir as tabelas de saúde preservadas e monitorar erros. Documentar descarte de arquivos exclusivos e do backup ao fim da retenção.

O SQL é idempotente e inclui históricos, publicações, curadorias e backups auxiliares do módulo, mais a meta calórica do perfil. Alergias, restrições clínicas, peso e atividade física são preservados. Rollback após exclusão requer restauração coordenada do backup com a versão compatível; reverter apenas código não recupera dados.

O histórico SQL anterior permanece para reprodutibilidade. Instalações novas aplicam a cadeia completa e terminam sem as estruturas de alimentação por meio da migration 075. Não modificar migrations já aplicadas.
