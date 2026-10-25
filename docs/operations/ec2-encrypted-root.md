# Proteção do disco e da memória do Gateway

O atendimento processa conteúdo sensível em memória. /tmp em tmpfs não impede paginação para swap; por isso o volume raiz também precisa de criptografia. O banco e os anexos persistentes desse fluxo ficam no RDS com criptografia própria. Expurgo do histórico ativo em 23h59 não substitui proteção do swap ou políticas de backup.

## Configuração desejada

- Volume raiz gp3 criptografado, preservando capacidade, IOPS e throughput existentes.
- Chave AWS gerenciada alias/aws/ebs; não criar chave customizada com mensalidade.
- Preservar instance ID, grupos de segurança, perfil IAM, zona e mapeamento do dispositivo raiz.
- Swap pode permanecer para preservar disponibilidade da t3.micro; deve estar no volume protegido.
- Serviço braga-gateway com LimitCORE=0. Handler systemd-coredump com Storage=none e ProcessSizeMax=0 para impedir persistência e processamento de dumps. A política do handler vale para o host inteiro.

Os drop-ins em infra/systemd são instalados pelo deploy.sh antes de reiniciar o serviço. Não incluem credenciais, transcrições ou dados clínicos.

## Migração operacional de volume existente

A troca do disco de inicialização existente usa a CLI EC2 com validações por etapa e registro dos IDs retornados. Não submeter uma instância produtiva criada fora de CloudFormation a substituição de stack apenas para trocar o volume. A operação é registrada como migração, não como provisionamento de um novo servidor.

1. Conferir instância, volume raiz atual, zona, parâmetros de desempenho, DeleteOnTermination e ausência de deploy concorrente. Confirmar saúde e inicialização automática de Gateway, cloudflared e SSM. Registrar os metadados sem ler conteúdo de atendimentos.
2. Instalar e verificar as políticas de core; parar a instância pela API com desligamento normal. Não usar força, hibernação ou destacamento com a instância em execução.
3. Com a instância stopped, criar snapshot consistente temporário do volume original e aguardar conclusão.
4. Criar volume a partir desse snapshot na mesma zona, Encrypted=true, KmsKeyId=alias/aws/ebs e mesmos size/type/IOPS/throughput. Não usar fast snapshot restore ou taxa de inicialização paga sem necessidade.
5. Aguardar volume available e confirmar chave/criptografia. Só então destacar o original, preservando-o para recuperação, e anexar o novo no mesmo dispositivo raiz.
6. Confirmar associação do novo volume, preservar DeleteOnTermination e iniciar a mesma instância. Um IP público automático pode mudar; verificar o acesso via domínio/túnel.
7. Conferir EC2 status checks, SSM Online, sistema montado no serial do novo volume, swap, serviços, /proc do Gateway com limite de core zero, configuração efetiva do coredump, health local/público, estado de conexão ao banco e ausência de erros do worker. Não usar conteúdo real de pacientes na validação.
8. Somente depois das verificações, eliminar volume original destacado e snapshot temporário. Registrar o estado final e confirmar que não sobraram cópias de migração cobradas.

## Recuperação

Antes da limpeza, se o novo disco não inicializar ou o Gateway não ficar saudável: parar a mesma instância, destacar o novo volume sem força, aguardar available, reanexar o original no dispositivo raiz registrado e iniciar. Não apagar o original nem o snapshot enquanto a validação estiver incompleta. Não executar comandos de recuperação com IDs presumidos; usar o registro da migração.

## Custos e limites

O novo disco mantém a configuração de armazenamento existente. Há custo temporário pelas cópias de volume/snapshot durante a migração. Remover as cópias após validação evita armazenamento adicional permanente. Chamadas KMS seguem a tabela vigente e a franquia compartilhada da conta. CloudTrail Event History registra operações de gerenciamento; esta entrega não habilita destinos adicionais de logs com cobrança.

## Referências

- [Criptografia de volume a partir de snapshot](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-examples.html).
- [Destacar volume raiz exige instância parada](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-detaching-volume.html).
- [Anexar volumes](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-attaching-volume.html).
- [Parada e início de EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html).
