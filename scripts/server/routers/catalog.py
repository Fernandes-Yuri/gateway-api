"""
Router de catálogo e preços de alimentos (Frente 1 / Mercado Livre API).
Endpoints:
- /api/catalog/grocery-prices: expõe os preços para o app Android
- /api/admin/grocery-prices*: endpoints de gestão manual consumidos pelo webservice
"""
import os
import secrets
from typing import Optional
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
    source: str = "meli"
    updated_at: Optional[str] = None


class ManualPriceUpdate(BaseModel):
    price: float = Field(..., gt=0, description="Preço unitário ou de embalagem em Reais (BRL)")


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
            res = await conn.execute(
                """
                UPDATE grocery_price_cache
                SET price_avg = $1,
                    price_min = $1,
                    price_max = $1,
                    source = 'manual',
                    needs_manual = FALSE,
                    updated_at = now()
                WHERE food_name = $2
                """,
                body.price, food_name
            )
            if res == "UPDATE 0":
                # Se não existia, insere
                await conn.execute(
                    """
                    INSERT INTO grocery_price_cache
                        (food_name, search_term, price_avg, price_min, price_max, source, needs_manual, updated_at)
                    VALUES ($1, $1, $2, $2, $2, 'manual', FALSE, now())
                    """,
                    food_name, body.price
                )
        return {"success": True, "food_name": food_name, "price": body.price}
    except Exception as e:
        raise HTTPException(500, f"Erro ao atualizar preço manual: {e}")


@router.post("/api/admin/grocery-prices/refresh")
async def trigger_grocery_prices_refresh(
    request: Request,
    _auth=Depends(require_service_token)
):
    """Dispara job assíncrono de atualização no Mercado Livre."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    import asyncio
    from services.meli_price_fetcher import atualizar_precos
    asyncio.create_task(atualizar_precos(_state.db_pool))
    return {"success": True, "message": "Atualização de preços iniciada em segundo plano."}
