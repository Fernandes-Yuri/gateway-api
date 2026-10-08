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
from api_identity import firebase_uid
from services.ingredient_catalog import nutrition_snapshot, resolve_ingredient
from services.grocery_contributions import GroceryPriceContribution, AVERAGES_SQL, UPSERT_SQL

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
    price_unit: Optional[Literal["kg", "L", "un"]] = None
    price: float = Field(..., gt=0, le=99999999.99, multiple_of=0.01, allow_inf_nan=False, description="Preço unitário ou de embalagem em Reais (BRL)")


_MANUAL_UPSERT = """
INSERT INTO grocery_price_cache
    (food_name, search_term, price_avg, price_min, price_max, source, results_count, needs_manual, updated_at)
VALUES ($1, $1, $2, $2, $2, 'manual', 0, FALSE, now())
ON CONFLICT (food_name) DO UPDATE SET
    price_avg = EXCLUDED.price_avg, price_min = EXCLUDED.price_min,
    price_max = EXCLUDED.price_max, source = 'manual', results_count = 0,
    needs_manual = FALSE, price_unit = NULL, updated_at = now()
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
                              (["food_name", "price"], ["price", "food_name"], ["food_name", "price", "price_unit"])), None)
            if delimiter is None:
                raise ValueError("Cabeçalho esperado: food_name,price")
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
            rows = list(reader)
        if not rows or len(rows) > 5000:
            raise ValueError("Informe entre 1 e 5000 linhas")
    except (ValueError, UnicodeError, csv.Error):
        raise HTTPException(422, "CSV ou JSON inválido. Use food_name e price.")
    valid, details, seen, units = [], [], set(), {}
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
            model = ManualPriceUpdate(price=value, price_unit=row.get("price_unit"))
            valid.append((name, Decimal(str(model.price))))
            seen.add(name)
            units[name] = model.price_unit
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
                    for name, _price in valid:
                        if units.get(name):
                            await conn.execute("UPDATE grocery_price_cache SET price_unit=$2 WHERE food_name=$1", name, units[name])
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
            async with conn.transaction():
                await conn.execute(_MANUAL_UPSERT, food_name, Decimal(str(body.price)))
                if body.price_unit:
                    await conn.execute("UPDATE grocery_price_cache SET price_unit=$2 WHERE food_name=$1", food_name, body.price_unit)
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


@router.post("/api/catalog/grocery-price-contributions")
async def contribute_grocery_price(body: GroceryPriceContribution, request: Request, uid=Depends(firebase_uid)):
    if not _state.db_pool:
        raise HTTPException(503, "Contribuições indisponíveis no momento.")
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                try:
                    ingredient = await resolve_ingredient(conn, body.food_name)
                    unit, price = body.normalized(ingredient)
                except ValueError as exc:
                    raise HTTPException(422, str(exc))
                # Serializa envios por pessoa antes de aplicar o limite diário.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "grocery-report:" + uid)
                profile_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=$1)", uid)
                if not profile_exists:
                    raise HTTPException(409, "Sincronize seu perfil antes de contribuir.")
                count = await conn.fetchval("SELECT COUNT(*) FROM grocery_price_contributions WHERE user_id=$1 AND created_at >= CURRENT_DATE", uid)
                if count >= 30:
                    raise HTTPException(429, "Limite diário de contribuições atingido. Tente amanhã.")
                # A chave legada do cache é exigida pela FK; não cria nem altera preço.
                await conn.execute("""INSERT INTO grocery_price_cache
                    (food_name,search_term,source,needs_manual,price_unit)
                    VALUES ($1,$1,'unavailable',TRUE,$2) ON CONFLICT(food_name) DO NOTHING""",
                    ingredient["name"], ingredient["unit"])
                await conn.execute(UPSERT_SQL, uid, ingredient["name"], body.amount_paid,
                    body.quantity, body.unit, unit, price, body.state, body.purchase_date, ingredient["slug"])
        return {"success": True, "unit": unit, "price_per_unit": round(float(price), 4),
                "message": "Contribuição recebida. Obrigado por ajudar!"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Não foi possível salvar a contribuição. Tente novamente depois.")


@router.get("/api/catalog/grocery-price-contributions/averages")
async def get_community_grocery_prices():
    if not _state.db_pool:
        raise HTTPException(503, "Referências colaborativas indisponíveis no momento.")
    try:
        async with _state.db_pool.acquire() as conn:
            rows = await conn.fetch(AVERAGES_SQL)
        return {"prices": [{"food_name": r["food_name"], "unit": r["unit"],
            "price_avg": round(float(r["price_avg"]), 4),
            "price_min": round(float(r["price_min"]), 4), "price_max": round(float(r["price_max"]), 4),
            "contributors": r["contributors"], "states_count": r["states_count"]} for r in rows],
            "minimum_contributors": 5, "window_days": 90,
            "notice": "Média das compras informadas pelos participantes; pode variar por região, loja e data."}
    except Exception:
        raise HTTPException(503, "Referências colaborativas indisponíveis no momento.")


@router.get("/api/catalog/grocery-ingredients")
async def get_grocery_ingredients():
    if not _state.db_pool:
        raise HTTPException(503, "Preços de referência indisponíveis no momento.")
    try:
        snapshot = await nutrition_snapshot(_state.db_pool, AVERAGES_SQL)
        return {key: snapshot[key] for key in ("version", "ingredients", "notice")}
    except Exception:
        raise HTTPException(503, "Preços de referência indisponíveis no momento.")


@router.get("/api/catalog/nutrition")
async def get_nutrition_catalog():
    """Snapshot completo: alimentos, ingredientes e política na mesma revisão do RDS."""
    if not _state.db_pool:
        raise HTTPException(503, "Catálogo nutricional indisponível no momento.")
    try:
        return await nutrition_snapshot(_state.db_pool, AVERAGES_SQL)
    except Exception:
        raise HTTPException(503, "Catálogo nutricional indisponível no momento.")
