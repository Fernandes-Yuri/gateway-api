# Encerramento de alimentação

A aplicação não lê nem grava mais no módulo. As rotas legadas retornam HTTP 410 sem acessar PostgreSQL. Catálogo e preços não recebem seeds ou jobs.

A limpeza destrutiva está separada do deploy automático. Antes de executá-la:

1. Publicar o backend e os clientes sem alimentação; conferir que erro 410 de catálogo não interrompe saúde/sincronização em clientes antigos, ou exigir atualização.
2. Executar `nutrition_inventory.sql` no ambiente alvo e revisar dependências não previstas, contagens e armazenamento externo de imagens/exports. O inventário não imprime registros pessoais.
3. Criar backup recuperável, registrar identificador, retenção e evidência de restauração. Não incluir dados ou credenciais no PR.
4. Apresentar SQL, ambiente, contagens e backup para confirmação da exclusão.
5. Na mesma sessão, definir `SET braga.allow_nutrition_purge = 'on'` e executar `retire_nutrition.sql` com parada em erro. A transação usa RESTRICT implícito; dependência inesperada aborta tudo, sem CASCADE.
6. Reexecutar o inventário, conferir as tabelas de saúde preservadas e monitorar erros. Documentar descarte de arquivos exclusivos e do backup ao fim da retenção.

O SQL é idempotente e inclui históricos, publicações, curadorias e backups auxiliares do módulo, mais a meta calórica do perfil. Alergias, restrições clínicas, peso e atividade física são preservados. Rollback após exclusão requer restauração coordenada do backup com a versão compatível; reverter apenas código não recupera dados.

O histórico SQL anterior permanece para reprodutibilidade. Após a aprovação operacional, promover este SQL para uma migração definitiva (com o próximo número disponível, sem alterar migrations aplicadas) e remover o gate manual nessa promoção. Até lá, uma instalação nova ainda cria as tabelas históricas, mas nenhum serviço as utiliza.
