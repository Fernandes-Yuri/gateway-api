"""Atualização semanal gratuita por IPCA sobre preços históricos existentes."""
import asyncio
import calendar
import json
import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import httpx
from services.grocery_baseline import fetch_baseline

_ROOT = Path(__file__).resolve().parent.parent
_SEED_PATH = _ROOT / "grocery_search_terms.json"
_DELAY_SECONDS = 0.5
_MAX_PRICE = 99999999.99
_LOG = logging.getLogger(__name__)
_LOCK = asyncio.Lock()
_UPSERT = """
INSERT INTO grocery_price_cache
    (food_name, search_term, price_avg, price_min, price_max, source,
     results_count, updated_at, needs_manual)
VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8)
ON CONFLICT (food_name) DO UPDATE SET
    search_term = EXCLUDED.search_term,
    price_avg = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_avg ELSE EXCLUDED.price_avg END,
    price_min = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_min ELSE EXCLUDED.price_min END,
    price_max = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.price_max ELSE EXCLUDED.price_max END,
    source = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.source ELSE EXCLUDED.source END,
    results_count = EXCLUDED.results_count,
    updated_at = CASE WHEN grocery_price_cache.source = 'manual' THEN grocery_price_cache.updated_at ELSE EXCLUDED.updated_at END,
    needs_manual = CASE WHEN grocery_price_cache.source = 'manual' THEN FALSE ELSE EXCLUDED.needs_manual END
WHERE grocery_price_cache.source <> 'manual'
  AND grocery_price_cache.updated_at IS NOT DISTINCT FROM $9::timestamptz
RETURNING food_name
"""
_BASELINE_ONLY_UPSERT = _UPSERT.replace(
    "WHERE grocery_price_cache.source <> 'manual'",
    "WHERE grocery_price_cache.source <> 'manual' AND grocery_price_cache.price_avg IS NULL",
).replace("results_count, updated_at, needs_manual)", "results_count, updated_at, needs_manual, price_unit)").replace(
    "now(), $8)", "now(), $8, 'kg')"
).replace("    results_count = EXCLUDED.results_count,", "    price_unit = 'kg', results_count = EXCLUDED.results_count,")


def _numero(value):
    text = str(value).strip().replace("R$", "").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    result = float(text)
    if not math.isfinite(result):
        raise ValueError("Número inválido")
    return result


def _money(value):
    if not math.isfinite(value) or not 0 < value <= _MAX_PRICE:
        raise ValueError("Preço fora dos limites")
    result = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if result <= 0:
        raise ValueError("Preço inferior a um centavo")
    return result


async def _fetch_ipca(client):
    try:
        response = await client.get("https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/63/p/last%20120", timeout=10.0)
        response.raise_for_status()
        rates = {}
        for row in response.json()[1:]:
            period = row["D3C"]
            if len(period) == 6 and period.isdigit():
                rate = _numero(row["V"]) / 100
                if -1 < rate < 1:
                    rates[period] = rate
        if not rates:
            raise ValueError("IPCA vazio")
        return rates, 0
    except Exception:
        _LOG.warning("[price_cascade] IPCA indisponível; usando taxa mensal configurada")
        return {}, 1


def _estimate_ipca(previous, now, rates):
    if previous is None or previous["price_avg"] is None:
        return None
    if previous["updated_at"] is None:
        raise ValueError("Histórico sem data; preservar preço existente")
    start = previous["updated_at"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if start > now:
        raise ValueError("Histórico com data futura; preservar preço existente")
    fallback = _numero(os.getenv("IPCA_MONTHLY_FALLBACK", "0.003"))
    if not -1 < fallback < 1:
        raise ValueError("IPCA_MONTHLY_FALLBACK inválido")
    factor, cursor = 1.0, start
    # Proporcional aos dias do mês: reexecuções semanais não aplicam um mês inteiro.
    while cursor < now:
        year, month = cursor.year, cursor.month
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
        stop = min(end, now)
        fraction = (stop - cursor).total_seconds() / (calendar.monthrange(year, month)[1] * 86400)
        factor *= (1 + rates.get(f"{year}{month:02d}", fallback)) ** fraction
        cursor = stop
    avg = _money(float(previous["price_avg"]) * factor)
    return {"avg": avg, "min": _money(float(previous["price_min"] or previous["price_avg"]) * factor),
            "max": _money(float(previous["price_max"] or previous["price_avg"]) * factor),
            "source": "estimated", "count": 0}


def _buscar_preco(previous, now, rates):
    return _estimate_ipca(previous, now, rates)


def _baseline_data(entry, now, rates):
    previous = {"price_avg": entry["price"], "price_min": entry["price"], "price_max": entry["price"],
                "updated_at": datetime.fromisoformat(entry["data"]).replace(tzinfo=timezone.utc)}
    return _estimate_ipca(previous, now, rates)


def _audit_baseline(entry):
    print("[grocery_baseline] Cadastrado: " + json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


async def atualizar_precos(db_pool):
    resumo = {"atualizados": 0, "sem_preco": 0, "erros": 0}
    if not db_pool:
        return {**resumo, "erros": 1}
    if _LOCK.locked():
        _LOG.info("[price_cascade] Atualização já em andamento")
        return resumo
    async with _LOCK:
        try:
            items = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
            rows = await db_pool.fetch("SELECT food_name, price_avg, price_min, price_max, source, updated_at FROM grocery_price_cache")
            history = {r["food_name"]: r for r in rows}
        except Exception:
            _LOG.error("[price_cascade] Falha ao carregar catálogo ou histórico")
            return {**resumo, "erros": 1}
        now = datetime.now(timezone.utc)
        print(f"[price_cascade] Iniciando atualização de {len(items)} itens...")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            rates, errors = await _fetch_ipca(client)
            resumo["erros"] += errors
            missing = [item for item in items if history.get(item["food_name"]) is None or
                       (history[item["food_name"]]["price_avg"] is None and history[item["food_name"]]["source"] != "manual")]
            baselines = {}
            if missing:
                baselines, errors = await fetch_baseline(client, missing, now)
                resumo["erros"] += errors
            for item in items:
                name = item["food_name"]
                previous = history.get(name)
                if previous is not None and previous["source"] == "manual":
                    continue
                await asyncio.sleep(_DELAY_SECONDS)
                try:
                    data = _buscar_preco(previous, now, rates)
                    entry = baselines.get(name)
                    if data is None and entry:
                        data = _baseline_data(entry, now, rates)
                    data = data or {"avg": None, "min": None, "max": None, "source": "cascade", "count": 0}
                    saved = await db_pool.fetchrow(_BASELINE_ONLY_UPSERT if entry else _UPSERT, name, item["search_term"], data["avg"],
                                                  data["min"], data["max"], data["source"], data["count"],
                                                  data["avg"] is None, previous["updated_at"] if previous else None)
                    if saved:
                        resumo["sem_preco" if data["avg"] is None else "atualizados"] += 1
                        if entry:
                            _audit_baseline({**entry, "price_importado": str(data["avg"])})
                except Exception:
                    resumo["erros"] += 1
                    _LOG.warning("[price_cascade] Erro ao processar item do catálogo")
        print(f"[price_cascade] Concluído: {resumo}")
        return resumo


async def seed_inicial(db_pool):
    if not db_pool:
        return
    try:
        items = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        await db_pool.executemany("""INSERT INTO grocery_price_cache (food_name, search_term, source, needs_manual)
                                  VALUES ($1, $2, 'cascade', TRUE) ON CONFLICT (food_name) DO NOTHING""",
                                  [(r["food_name"], r["search_term"]) for r in items])
        _LOG.info("[price_cascade] Seed inicial executado para %s itens", len(items))
        # Preenchimento inicial em background; não bloquear a inicialização da API.
        asyncio.create_task(preencher_baseline(db_pool))
    except Exception:
        _LOG.warning("[price_cascade] Aviso no seed inicial")


async def preencher_baseline(db_pool):
    """Preenche apenas nulos; executado na inicialização e refeito pelo job semanal."""
    if _LOCK.locked():
        return
    async with _LOCK:
        try:
            items = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
            rows = await db_pool.fetch("SELECT food_name, price_avg, price_min, price_max, source, updated_at FROM grocery_price_cache")
            history = {row["food_name"]: row for row in rows}
            missing = [item for item in items if history.get(item["food_name"]) is None or
                       (history[item["food_name"]]["price_avg"] is None and history[item["food_name"]]["source"] != "manual")]
            if not missing:
                return
            now = datetime.now(timezone.utc)
            async with httpx.AsyncClient(follow_redirects=True) as client:
                baselines, errors = await fetch_baseline(client, missing, now)
                rates, ipca_errors = await _fetch_ipca(client)
            imported = 0
            for item in missing:
                entry = baselines.get(item["food_name"])
                if not entry:
                    continue
                previous = history.get(item["food_name"])
                try:
                    data = _baseline_data(entry, now, rates)
                    saved = await db_pool.fetchrow(_BASELINE_ONLY_UPSERT, item["food_name"], item["search_term"],
                                                  data["avg"], data["min"], data["max"], "estimated", 0,
                                                  False, previous["updated_at"] if previous else None)
                    if saved:
                        imported += 1
                        _audit_baseline({**entry, "price_importado": str(data["avg"])})
                except Exception:
                    errors += 1
            print(f"[grocery_baseline] Concluído: importados={imported} sem_fonte={len(missing) - imported} erros={errors + ipca_errors}")
        except Exception:
            print("[grocery_baseline] Falha no preenchimento inicial; preços existentes preservados")
