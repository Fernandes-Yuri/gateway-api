# Pesquisa — fine-tuning e dataset (Fase 5)

**Estado: pesquisa, não operacional. Nada aqui roda em produção.**

Esta pasta existe porque estes scripts viviam em `scripts/server/`, que é sincronizado
inteiro para `app/` no deploy (`deploy.sh`, rsync `scripts/server/ → app/`). Eram
código de pesquisa empacotado como runtime — dívida DT-024.

Verificado antes da separação: nenhum módulo de `scripts/server/` importava estes
arquivos; só os próprios testes os usavam, e vieram junto.

## Arquivos

| Arquivo | O que é |
|---|---|
| `generate_fine_tuning_seed.py` | Gera a semente de dataset no formato two-step (`SEED_CLASSIFY` / `SEED_SPEAK`) |
| `curate_dataset.py` | Curadoria: separa amostras limpas das que cristalizariam delírio (`audit()`) |
| `train_fine_tuning_unsloth.py` | Pipeline QLoRA 4-bit com Unsloth sobre Qwen 2.5 7B/14B |
| `test_seed.py` | Trava de coerência da semente: nada gerado é quarentenável |
| `test_curate.py` | Testes da curadoria, incluindo o contrato do agente B0 (`CurateContractTests`) |

## Requisitos (só para retomar a pesquisa)

- `train_fine_tuning_unsloth.py`: GPU com ≥8 GB VRAM (Colab T4, RunPod, Vast.ai ou RTX local)
  e `pip install unsloth "xformers<0.0.28" "trl<0.9.0" peft accelerate bitsandbytes`.
- Demais scripts: stdlib de Python; `generate_fine_tuning_seed.py` usa `orb_llm` quando
  disponível e cai para prompt embutido quando roda avulso.

## Como correr os testes

```bash
PYTHONPATH=scripts/server python3 -m unittest discover research/ "test_*.py"
```

O `PYTHONPATH` é necessário porque a curadoria consulta o catálogo de ações do runtime
(`orb_actions`) dentro de `try/except`: sem esse caminho, a curadoria roda degradada e o
teste `action-sem-executor` falha.

O CI (`Validar alterações` e `Deploy to EC2`) executa este passo para garantir que a
separação não quebrou imports.

## Condições de eventual retorno

Antes de trazer qualquer coisa de volta para `scripts/server/`:

1. Decisão registada em `Decisões/` no Cérebro: SLM local versus IA cloud no gateway —
   o abandono do SLM é relato do perfil público, sem ensaio original recuperado.
2. Só volta para a árvore operacional o que for efetivamente runtime, com teste e gate.
3. Curadoria e geração de dataset continuam fora do runtime, por definição.
4. Nenhum dado real de usuário entra em dataset de treino: usar amostras sintéticas.
