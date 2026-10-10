"""Módulo de atualização de preços legados (aposentado na migração 045).

Preços agora são geridos pelo painel administrativo, contribuições da comunidade
e rotina semanal de cascata de índices em price_cascade.
"""


async def atualizar_precos(db_pool):
    """Substituído pela cascata de preços em price_cascade."""
    return {"atualizados": 0, "sem_preco": 0, "erros": 0}


async def seed_inicial(db_pool):
    """Seed inicial legado."""
    pass
