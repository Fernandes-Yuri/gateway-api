# Guia de Contribuição — Braga Saúde — AI Gateway API

Obrigado pelo interesse em contribuir com o **Braga Saúde**! Este guia define o fluxo de trabalho, padrões de código, governança de branches e boas práticas de segurança para este repositório.

---

## 🔒 1. Segurança e LGPD em Primeiro Lugar

Trabalhamos com dados sensíveis de saúde e cuidado familiar:
* **NUNCA commite segredos:** Nunca commite arquivos .env, credenciais de acesso AWS (chaves de acesso, pares PEM), tokens de serviço ou strings de conexão com senha do RDS.
* Arquivos de configuração local (`.env`, `keystore.properties`, etc.) devem ser sempre mantidos fora do controle de versão via `.gitignore`.
* Se identificar qualquer vazamento acidental de credencial, notifique imediatamente a equipe antes de qualquer push.

---

## 🌿 2. Fluxo de Branches

Todas as contribuições devem ser feitas em **branches isoladas**:

* `master`: Branch principal de produção estável.
* `feature/<nome-da-funcionalidade>`: Para novas funcionalidades ou melhorias de produto.
* `fix/<descricao-do-bug>`: Para correções de bugs identificados.
* `chore/<manutencao-ou-docs>`: Para tarefas de documentação, refatoração e infraestrutura.

### Criando sua branch:
```bash
git checkout master
git pull origin master
git checkout -b feature/minha-nova-funcionalidade
```

---

## 📝 3. Padrão de Commits (Conventional Commits)

Utilizamos o padrão de commits semânticos:

* `feat(...)`: Nova funcionalidade para o usuário ou sistema.
* `fix(...)`: Correção de um bug ou erro em tempo de execução.
* `refactor(...)`: Mudança no código que não altera comportamento externo.
* `perf(...)`: Melhoria de desempenho ou consumo de memória.
* `test(...)`: Adição ou refatoração de testes automatizados.
* `chore(...)`: Atualização de dependências, documentação, CI/CD ou configurações.

Exemplo:
```bash
git commit -m "feat(cards): adiciona ordenacao inteligente por proximidade de dose"
```

---

## 🧪 4. Validação Local

Antes de abrir um Pull Request, execute os comandos de verificação local para garantir que a suite de testes e build estão passando:

```bash
# Executar testes unitários:
pytest

# Executar build / lint:
ruff check .
```

---

## 🔀 5. Processo de Pull Request

1. Garanta que sua branch esteja atualizada com a `master`:
   ```bash
   git fetch origin
   git rebase origin/master
   ```
2. Abra o Pull Request no GitHub apontando para a branch `master`.
3. Preencha o checklist do **Pull Request Template** descrevendo claramente o que foi alterado.
4. Aguarde a revisão de código e a execução dos gates do GitHub Actions.
