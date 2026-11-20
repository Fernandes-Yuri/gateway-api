# Conta de teste B2B-31

Rotina manual, sem hook no deploy, migrations ou startup. Executar somente após
o CI e o deploy do commit aprovado. O Python/venv e o ambiente são os mesmos do
gateway na EC2; `DATABASE_URL` e `FIREBASE_SERVICE_ACCOUNT_PATH` devem ser
injetados pelo runtime, nunca copiados para comandos, logs ou Git.

```bash
venv/bin/python scripts/maintenance/seed_b2b_demo_account.py --check
venv/bin/python scripts/maintenance/seed_b2b_demo_account.py
```

O preflight consulta as três contas existentes em Firebase Auth (`braga-saude`):
`testeb2b@teste.com`, `testebraga1@proton.me`, `testebraga2@proton.me`. Ausência,
conta desativada, emulador, projeto incorreto, paciente sem perfil ou colisão de
programa interrompem a rotina. Não há criação/edição de identidades, senhas,
claims ou usuários fictícios. O titular pode ganhar seu perfil PostgreSQL
empresarial, sem obrigar preenchimento pessoal.

O seed configura `TEST-B2B1`, `APPROVED`, 50 vagas, `CLINICA_GERAL`, cor `#00695C`,
logo público Braga (preserva logo existente), sete módulos ativos e FAQ de teste.
Os pacientes recebem quatro e duas categorias, respectivamente. O vínculo
familiar só habilita o compartilhamento do primeiro quando o segundo já é seu
cuidador ativo, conforme a direção autorizada pelo backend.

Cria uma solicitação `PENDING`, um agendamento `CONFIRMED` para daqui a sete dias,
lembretes `ROUTINE`/`HYDRATION` e chats `WAITING_HUMAN`/`AI_ACTIVE`. **Ambos os
lembretes têm recibo e estão lidos**, conforme o escopo confirmado de B2B-31:
neste schema recibo não representa entrega pendente, mas reconhecimento.
Não dispara push, e-mail nem chamadas de IA; as mensagens iniciais são demonstrações.

Se passos ou hidratação do primeiro paciente não tiverem registros nos últimos
sete dias, adiciona somente três pontos pequenos na série ausente, sem atualizar
linhas anteriores. Hidratação recebe `confirmed_via=b2b_demo`; os UUIDs desses
registros e a auditoria `DEMO_SEEDED` identificam o complemento. Nunca inventa
pressão, glicemia, peso, altura ou medicamentos. A tabela de biometria vigente é
`biometrics`; `biometries` foi aposentada pela migration 088. As medições reais
continuam disponíveis no drill-down conforme o consentimento.

Reexecuções reutilizam IDs e preservam leitura, mensagens e andamento dos
agendamentos/chats. Consentimentos e configuração de teste são reaplicados.
As datas não são empurradas para o futuro a cada execução; para reconstruir os
cenários depois de usá-los ou após expirar a retenção, executar explicitamente:

```bash
venv/bin/python scripts/maintenance/seed_b2b_demo_account.py --reset
venv/bin/python scripts/maintenance/seed_b2b_demo_account.py
```

`--reset` **apenas limpa**, em transação, os vínculos, lembretes (e seus recibos),
agendamentos, mensagens e threads do `program_id` de `TEST-B2B1`, após conferir
o titular no Firebase. Preserva o programa, a auditoria, perfis, métricas
(inclusive complemento já inserido), vínculos familiares, medicamentos,
consultas pessoais e todos os outros programas. Não recria dados automaticamente.

Os testes `scripts/server/test_seed_b2b_demo_account.py` rodam no CI existente,
com PostgreSQL 16 efêmero. Cobrem identidades, colisão entre programas, rollback,
reset repetido, preservação de dados pessoais, séries recentes, consentimentos
e continuidade de uso após repetir o seed. Fixtures sintéticas existem apenas
nesse banco efêmero; o executável de manutenção sempre resolve contas reais.
