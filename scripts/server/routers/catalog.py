"""
Router de catálogo e preços de alimentos (estimativa nacional por IPCA).
Endpoints:
- /api/catalog/grocery-prices: expõe os preços para o app Android
- /api/admin/grocery-prices*: endpoints de gestão manual consumidos pelo webservice
"""
import os
import secrets
import csv
import io
import json
from decimal import Decimal
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from security_defense import get_client_ip

router = APIRouter()


class _AppState:
    """Estado injetado pelo server.py via configure_state()."""
    db_pool = None


_state = _AppState()


def configure_state(*, db_pool=None):
    _state.db_pool = db_pool


def _service_token() -> str:
    return os.getenv("SERVICE_TOKEN", "").strip()


async def require_service_token(request: Request):
    """Exige Bearer <SERVICE_TOKEN> para endpoints administrativos."""
    token = _service_token()
    if not token:
        raise HTTPException(503, "Portal administrativo indisponível: SERVICE_TOKEN não configurado.")
    supplied = request.headers.get("Authorization", "")
    if not supplied.startswith("Bearer "):
        print(f"[ADMIN-CATALOG] Acesso negado (sem Bearer) {get_client_ip(request)} {request.url.path}")
        raise HTTPException(401, "Não autorizado.")
    candidate = supplied[len("Bearer "):]
    if not secrets.compare_digest(candidate, token):
        print(f"[ADMIN-CATALOG] Token inválido {get_client_ip(request)} {request.url.path}")
        raise HTTPException(401, "Não autorizado.")


# Modelos Pydantic
class GroceryPriceItemDto(BaseModel):
    food_name: str
    price_avg: Optional[float] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    source: str = "cascade"
    updated_at: Optional[str] = None


class ManualPriceUpdate(BaseModel):
    price: float = Field(..., gt=0, le=99999999.99, multiple_of=0.01, allow_inf_nan=False, description="Preço unitário ou de embalagem em Reais (BRL)")


_MANUAL_UPSERT = """
INSERT INTO grocery_price_cache
    (food_name, search_term, price_avg, price_min, price_max, source, results_count, needs_manual, updated_at)
VALUES ($1, $1, $2, $2, $2, 'manual', 0, FALSE, now())
ON CONFLICT (food_name) DO UPDATE SET
    price_avg = EXCLUDED.price_avg, price_min = EXCLUDED.price_min,
    price_max = EXCLUDED.price_max, source = 'manual', results_count = 0,
    needs_manual = FALSE, updated_at = now()
"""

_BASELINE_UPSERT = _MANUAL_UPSERT.replace("'manual'", "'estimated'") + "\nWHERE grocery_price_cache.source <> 'manual' RETURNING food_name"


@router.post("/api/admin/grocery-prices/import-csv")
async def import_grocery_prices(request: Request, mode: Literal["manual", "baseline"] = "manual", _auth=Depends(require_service_token)):
    """Importa preços manuais em lote por CSV UTF-8 ou lista JSON."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Importação limitada a 1 MiB.")
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        text = raw.decode("utf-8-sig")
        if content_type == "application/json":
            rows = json.loads(text)
            if not isinstance(rows, list):
                raise ValueError("Esperada uma lista JSON")
        else:
            # Detectar pelo cabeçalho: uma linha com colunas extras não invalida as demais.
            delimiter = next((sep for sep in (",", ";")
                              if next(csv.reader(io.StringIO(text), delimiter=sep), []) in
                              (["food_name", "price"], ["price", "food_name"])), None)
            if delimiter is None:
                raise ValueError("Cabeçalho esperado: food_name,price")
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
            rows = list(reader)
        if not rows or len(rows) > 5000:
            raise ValueError("Informe entre 1 e 5000 linhas")
    except (ValueError, UnicodeError, csv.Error):
        raise HTTPException(422, "CSV ou JSON inválido. Use food_name e price.")
    valid, details, seen = [], [], set()
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict):
                raise ValueError("Linha inválida")
            name = row.get("food_name")
            if not isinstance(name, str) or not name.strip() or len(name) > 500:
                raise ValueError("food_name inválido")
            name = name.strip()
            if name in seen:
                raise ValueError("food_name duplicado")
            value = row.get("price")
            if isinstance(value, bool) or row.get(None) is not None:
                raise ValueError("Preço ou colunas inválidos")
            if isinstance(value, str):
                value = value.strip().replace(",", ".")
            model = ManualPriceUpdate(price=value)
            valid.append((name, Decimal(str(model.price))))
            seen.add(name)
        except (ValueError, TypeError):
            details.append({"linha": index, "erro": "Nome, preço ou colunas inválidos; preço deve ser positivo com até duas casas decimais e nome único."})
    if valid:
        try:
            async with _state.db_pool.acquire() as conn:
                async with conn.transaction():
                    if mode == "manual":
                        await conn.executemany(_MANUAL_UPSERT, valid)
                    else:
                        imported = []
                        for name, price in valid:
                            saved = await conn.fetchrow(_BASELINE_UPSERT, name, price)
                            if saved:
                                imported.append((name, price))
                            else:
                                details.append({"food_name": name, "erro": "Preço manual preservado."})
                        valid = imported
        except Exception:
            raise HTTPException(500, "Falha ao importar preços; nenhuma linha do lote foi gravada.")
    print(f"[ADMIN-CATALOG] Importação {mode}: importados={len(valid)} erros={len(details)}")
    return {"importados": len(valid), "erros": len(details), "detalhes": details}


# ---- Endpoint público/app -----------------------------------------------

@router.get("/api/catalog/grocery-prices")
async def get_grocery_prices(request: Request):
    """
    Retorna a lista de alimentos e preços médios estimados para a lista de compras.
    Consumido pelo aplicativo Android.
    """
    if not _state.db_pool:
        return {"prices": []}

    try:
        async with _state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT food_name, price_avg, price_min, price_max, source, updated_at
                FROM grocery_price_cache
                ORDER BY food_name ASC
                """
            )
        prices = [
            {
                "food_name": r["food_name"],
                "price_avg": float(r["price_avg"]) if r["price_avg"] is not None else None,
                "price_min": float(r["price_min"]) if r["price_min"] is not None else None,
                "price_max": float(r["price_max"]) if r["price_max"] is not None else None,
                "source": r["source"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
        return {"prices": prices}
    except Exception as e:
        print(f"[CATALOG] Erro ao listar preços: {e}")
        return {"prices": []}


# ---- Endpoints administrativos (webservice) ------------------------------

@router.get("/api/admin/grocery-prices")
async def list_admin_grocery_prices(
    request: Request,
    _auth=Depends(require_service_token)
):
    """Lista todos os itens com métricas de preço, fonte e status."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT food_name, search_term, price_avg, price_min, price_max,
                       source, results_count, updated_at, needs_manual
                FROM grocery_price_cache
                ORDER BY needs_manual DESC, food_name ASC
                """
            )
        items = [
            {
                "food_name": r["food_name"],
                "search_term": r["search_term"],
                "price_avg": float(r["price_avg"]) if r["price_avg"] is not None else None,
                "price_min": float(r["price_min"]) if r["price_min"] is not None else None,
                "price_max": float(r["price_max"]) if r["price_max"] is not None else None,
                "source": r["source"],
                "results_count": r["results_count"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
                "needs_manual": bool(r["needs_manual"]),
            }
            for r in rows
        ]
        return {"success": True, "total": len(items), "items": items}
    except Exception as e:
        raise HTTPException(500, f"Erro ao consultar itens: {e}")


@router.get("/api/admin/grocery-prices/missing")
async def list_missing_grocery_prices(
    request: Request,
    _auth=Depends(require_service_token)
):
    """Lista itens sem preço ou marcados como pendentes de inserção manual."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT food_name, search_term, price_avg, price_min, price_max,
                       source, results_count, updated_at, needs_manual
                FROM grocery_price_cache
                WHERE needs_manual = TRUE OR price_avg IS NULL
                ORDER BY food_name ASC
                """
            )
        items = [
            {
                "food_name": r["food_name"],
                "search_term": r["search_term"],
                "price_avg": float(r["price_avg"]) if r["price_avg"] is not None else None,
                "price_min": float(r["price_min"]) if r["price_min"] is not None else None,
                "price_max": float(r["price_max"]) if r["price_max"] is not None else None,
                "source": r["source"],
                "results_count": r["results_count"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
                "needs_manual": bool(r["needs_manual"]),
            }
            for r in rows
        ]
        return {"success": True, "total": len(items), "items": items}
    except Exception as e:
        raise HTTPException(500, f"Erro ao consultar pendências: {e}")


@router.put("/api/admin/grocery-prices/{food_name}")
async def update_grocery_price_manual(
    food_name: str,
    body: ManualPriceUpdate,
    request: Request,
    _auth=Depends(require_service_token)
):
    """Atualiza manualmente o preço de um alimento e desmarca pendência."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            await conn.execute(_MANUAL_UPSERT, food_name, Decimal(str(body.price)))
        return {"success": True, "food_name": food_name, "price": body.price}
    except Exception as e:
        raise HTTPException(500, f"Erro ao atualizar preço manual: {e}")


@router.post("/api/admin/grocery-prices/refresh")
async def trigger_grocery_prices_refresh(
    request: Request,
    _auth=Depends(require_service_token)
):
    """Dispara job assíncrono de atualização por IPCA."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    import asyncio
    from services.price_cascade import atualizar_precos
    asyncio.create_task(atualizar_precos(_state.db_pool))
    return {"success": True, "message": "Atualização de preços iniciada em segundo plano."}
