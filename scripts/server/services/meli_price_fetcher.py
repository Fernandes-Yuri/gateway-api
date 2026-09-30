"""
Serviço de atualização de preços de alimentos via API pública do Mercado Livre.
Consulta a API /sites/MLB/search?q=...&category=MLB1246 (Alimentos e Bebidas)
para cada alimento do catálogo e armazena médias de preço no RDS.

Job agendado: toda segunda-feira às 03:00 BRT.
Fallback: itens sem resultado são marcados com needs_manual=TRUE.
"""
import asyncio
import json
from pathlib import Path
from typing import Dict, Any

import httpx

_BASE_URL = "https://api.mercadolibre.com/sites/MLB/search"
_CATEGORY = "MLB1246"  # Alimentos e Bebidas
_LIMIT = 10
_DELAY_SECONDS = 0.5
_SEED_PATH = Path(__file__).parent.parent / "grocery_search_terms.json"


async def _buscar_preco_meli(client: httpx.AsyncClient, search_term: str) -> Dict[str, Any]:
    """Busca preços no Mercado Livre e retorna estatísticas (avg, min, max, count)."""
    try:
        params = {"q": search_term, "category": _CATEGORY, "limit": _LIMIT}
        resp = await client.get(_BASE_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return {"avg": None, "min": None, "max": None, "count": 0}
        prices = [r["price"] for r in results if r.get("price") and r["price"] > 0]
        if not prices:
            return {"avg": None, "min": None, "max": None, "count": 0}
        # Remove outliers via IQR se houver ao menos 4 preços
        if len(prices) >= 4:
            prices_sorted = sorted(prices)
            n = len(prices_sorted)
            q1 = prices_sorted[n // 4]
            q3 = prices_sorted[(3 * n) // 4]
            iqr = q3 - q1
            lo = q1 - 1.5 * iqr
            hi = q3 + 1.5 * iqr
            filtered = [p for p in prices if lo <= p <= hi]
            if not filtered:
                filtered = prices
        else:
            filtered = prices

        return {
            "avg": round(sum(filtered) / len(filtered), 2),
            "min": round(min(filtered), 2),
            "max": round(max(filtered), 2),
            "count": len(filtered),
        }
    except Exception as e:
        print(f"[meli_price_fetcher] Erro ao buscar '{search_term}': {e}")
        return {"avg": None, "min": None, "max": None, "count": 0}


async def atualizar_precos(db_pool) -> Dict[str, int]:
    """
    Ponto de entrada do job semanal.
    Lê os termos de busca do seed JSON ou do banco, consulta o Meli e salva no RDS.
    Retorna resumo: {"atualizados": N, "sem_preco": M, "erros": K}
    """
    if not _SEED_PATH.exists():
        print(f"[meli_price_fetcher] Arquivo de termos não encontrado: {_SEED_PATH}")
        return {"atualizados": 0, "sem_preco": 0, "erros": 1}

    try:
        with open(_SEED_PATH, "r", encoding="utf-8") as f:
            itens = json.load(f)
    except Exception as e:
        print(f"[meli_price_fetcher] Falha ao carregar termos de busca: {e}")
        return {"atualizados": 0, "sem_preco": 0, "erros": 1}

    atualizados = 0
    sem_preco = 0
    erros = 0

    print(f"[meli_price_fetcher] Iniciando atualização de {len(itens)} itens...")

    async with httpx.AsyncClient() as client:
        for item in itens:
            food_name = item["food_name"]
            search_term = item["search_term"]
            await asyncio.sleep(_DELAY_SECONDS)
            dados = await _buscar_preco_meli(client, search_term)
            needs_manual = dados["avg"] is None
            try:
                if db_pool:
                    await db_pool.execute(
                        """
                        INSERT INTO grocery_price_cache
                            (food_name, search_term, price_avg, price_min, price_max,
                             source, results_count, updated_at, needs_manual)
                        VALUES ($1, $2, $3, $4, $5, 'meli', $6, now(), $7)
                        ON CONFLICT (food_name) DO UPDATE SET
                            search_term    = EXCLUDED.search_term,
                            price_avg      = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_avg ELSE EXCLUDED.price_avg END,
                            price_min      = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_min ELSE EXCLUDED.price_min END,
                            price_max      = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_max ELSE EXCLUDED.price_max END,
                            source         = grocery_price_cache.source,
                            results_count  = EXCLUDED.results_count,
                            updated_at     = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.updated_at ELSE now() END,
                            needs_manual   = CASE WHEN grocery_price_cache.source = 'manual' THEN FALSE ELSE EXCLUDED.needs_manual END
                        """,
                        food_name, search_term,
                        dados["avg"], dados["min"], dados["max"],
                        dados["count"], needs_manual,
                    )
                if needs_manual:
                    sem_preco += 1
                else:
                    atualizados += 1
            except Exception as e:
                erros += 1
                print(f"[meli_price_fetcher] Erro ao salvar '{food_name}': {e}")

    resumo = {"atualizados": atualizados, "sem_preco": sem_preco, "erros": erros}
    print(f"[meli_price_fetcher] Concluído: {resumo}")
    return resumo


async def seed_inicial(db_pool) -> None:
    """
    Popula a tabela com os termos de busca na inicialização se ainda não existirem.
    Não busca preços remotamente — apenas garante que todos os food_name existam na tabela.
    """
    if not _SEED_PATH.exists() or not db_pool:
        return
    try:
        with open(_SEED_PATH, "r", encoding="utf-8") as f:
            itens = json.load(f)
        for item in itens:
            await db_pool.execute(
                """
                INSERT INTO grocery_price_cache (food_name, search_term, needs_manual)
                VALUES ($1, $2, TRUE)
                ON CONFLICT (food_name) DO NOTHING
                """,
                item["food_name"], item["search_term"],
            )
        print(f"[meli_price_fetcher] Seed inicial executado para {len(itens)} itens na grocery_price_cache.")
    except Exception as e:
        print(f"[meli_price_fetcher] Aviso no seed inicial: {e}")
