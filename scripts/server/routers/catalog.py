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
from asyncpg.exceptions import ForeignKeyViolationError
from decimal import Decimal
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from security_defense import get_client_ip
from api_identity import firebase_uid
from services import ingredient_admin
from services.ingredient_catalog import (admin_price_rows, load_ingredients, key, nutrition_snapshot,
                                        resolve_ingredient, sale_to_canonical, sale_unit_allowed,
                                        sale_unit_options)
from services.grocery_contributions import GroceryPriceContribution, AVERAGES_SQL, UPSERT_SQL

from schemas.grocery_cards import GroceryRetailCardOut, BasketComparisonRequest, BasketComparisonResult
from services.grocery_cards import load_cards, compare_basket
from services.grocery_retail_admin import save_record
from schemas.grocery_cards import GroceryAdminMutation

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


class IngredientPriceUpdate(BaseModel):
    price: float = Field(..., gt=0, le=99999999.99, multiple_of=0.01, allow_inf_nan=False, description="Preço por unidade do ingrediente (kg, L ou un) em Reais (BRL)")
    price_unit: Optional[Literal["kg", "L", "un"]] = None
    sale_price: Optional[float] = Field(default=None, gt=0, le=99999999.99, multiple_of=0.01, allow_inf_nan=False, description="Preço da embalagem comercial em Reais (BRL)")
    sale_size: Optional[float] = Field(default=None, gt=0, le=1000000, allow_inf_nan=False, description="Quantidade na embalagem comercial (ex.: 300 para 300 g)")
    sale_unit: Optional[Literal["g", "kg", "ml", "L", "un", "dz"]] = None


_MANUAL_UPSERT = """
INSERT INTO grocery_price_cache
    (food_name, search_term, price_avg, price_min, price_max, source, results_count, needs_manual, updated_at)
VALUES ($1, $1, $2, $2, $2, 'manual', 0, FALSE, now())
ON CONFLICT (food_name) DO UPDATE SET
    price_avg = EXCLUDED.price_avg, price_min = EXCLUDED.price_min,
    price_max = EXCLUDED.price_max, source = 'manual', results_count = 0,
    needs_manual = FALSE, price_unit = NULL, updated_at = now()
"""

# Preço manual ancorado no ingrediente do catálogo: a chave é o nome canônico e
# a unidade do ingrediente, para o enriquecimento do app reconhecer a linha.
# $5..$7 são a embalagem comercial informada pelo administrador (opcional);
# price_avg continua na unidade canônica, para não desalinhar app e comunidade.
_MANUAL_INGREDIENT_UPSERT = """
INSERT INTO grocery_price_cache
    (food_name, search_term, price_avg, price_min, price_max, source, results_count, needs_manual, updated_at, price_unit,
     sale_price, sale_size, sale_unit)
VALUES ($1, $2, $3, $3, $3, 'manual', 0, FALSE, now(), $4, $5, $6, $7)
ON CONFLICT (food_name) DO UPDATE SET
    price_avg = EXCLUDED.price_avg, price_min = EXCLUDED.price_min,
    price_max = EXCLUDED.price_max, source = 'manual', results_count = 0,
    needs_manual = FALSE, price_unit = EXCLUDED.price_unit, updated_at = now(),
    sale_price = EXCLUDED.sale_price, sale_size = EXCLUDED.sale_size,
    sale_unit = EXCLUDED.sale_unit
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
                        # Importação legada não recebe embalagem: descarta a
                        # anterior para não deixar sale_* órfão junto de um
                        # preço novo ou de um price_unit trocado (violaria o
                        # CHECK de dimensão da migração 048).
                        await conn.execute(
                            "UPDATE grocery_price_cache SET sale_price=NULL, sale_size=NULL, sale_unit=NULL WHERE food_name=$1",
                            name)
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
    """Lista o catálogo de ingredientes com preço resolvido (manual > comunidade > estimado)."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction(readonly=True, isolation="repeatable_read"):
                items = await admin_price_rows(conn, AVERAGES_SQL)
        return {"success": True, "total": len(items), "items": items}
    except Exception as e:
        raise HTTPException(500, f"Erro ao consultar itens: {e}")


@router.get("/api/admin/grocery-prices/missing")
async def list_missing_grocery_prices(
    request: Request,
    _auth=Depends(require_service_token)
):
    """Lista ingredientes sem preço resolvido, pendentes de definição manual."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction(readonly=True, isolation="repeatable_read"):
                items = [row for row in await admin_price_rows(conn, AVERAGES_SQL) if row["needs_manual"]]
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
                # Este endpoint não recebe embalagem, então descarta a anterior:
                # manter sale_* junto de um price_unit trocado violaria o CHECK
                # de dimensão, e manter junto de um preço novo mostraria um
                # pacote que não corresponde mais ao valor gravado.
                await conn.execute(
                    "UPDATE grocery_price_cache SET sale_price=NULL, sale_size=NULL, sale_unit=NULL WHERE food_name=$1",
                    food_name)
                if body.price_unit:
                    await conn.execute("UPDATE grocery_price_cache SET price_unit=$2 WHERE food_name=$1", food_name, body.price_unit)
        return {"success": True, "food_name": food_name, "price": body.price}
    except Exception as e:
        raise HTTPException(500, f"Erro ao atualizar preço manual: {e}")


def _sale_triplet(body):
    """Campos de embalagem juntos ou ausentes; devolve a tupla normalizada.

    A embalagem é opcional, mas os três campos andam em combo: preço de
    embalagem sem tamanho ou unidade não dá para converter.
    """
    provided = [body.sale_price is not None, body.sale_size is not None, body.sale_unit is not None]
    if any(provided) and not all(provided):
        raise ValueError("Informe preço, tamanho e unidade da embalagem juntos, ou nenhum deles.")
    if not all(provided):
        return None, None, None
    return Decimal(str(body.sale_price)), Decimal(str(body.sale_size)), body.sale_unit


def _import_sale_triplet(row, ingredient):
    """Extrai a embalagem de uma linha de importação (CSV ou JSON).

    Aceita vírgula como separador decimal, igual ao preço. Os três campos
    andam juntos ou ausentes, e a unidade precisa caber na dimensão do
    ingrediente — o mesmo contrato do PUT individual.
    """
    raw_price = row.get("sale_price")
    raw_size = row.get("sale_size")
    sale_unit = row.get("sale_unit")
    provided = [v is not None and str(v).strip() != "" for v in (raw_price, raw_size, sale_unit)]
    if not any(provided):
        return None, None, None
    if not all(provided):
        raise ValueError("Informe preço, tamanho e unidade da embalagem juntos.")
    sale_price = Decimal(str(raw_price).strip().replace(",", "."))
    sale_size = Decimal(str(raw_size).strip().replace(",", "."))
    sale_unit = str(sale_unit).strip()
    if not sale_unit_allowed(ingredient["unit"], sale_unit):
        raise ValueError(f"Unidade de venda deve ser uma de {', '.join(sale_unit_options(ingredient['unit']))}.")
    return sale_price, sale_size, sale_unit


def _normalized_price(body, ingredient, sale_price, sale_size, sale_unit):
    """Preço na unidade canônica do ingrediente.

    Com embalagem declarada, o valor gravado vem da conversão (fonte de
    verdade é o que o admin digitou para o pacote), não do `price` enviado
    pelo cliente, que pode trazer arredondamento do navegador.
    """
    if sale_unit is None:
        return Decimal(str(body.price))
    factor = sale_to_canonical(sale_size, sale_unit, ingredient["unit"])
    normalized = sale_price / factor
    if normalized <= 0 or normalized > Decimal("99999999.99"):
        raise ValueError("Preço da embalagem converte para um valor fora do intervalo aceito.")
    return normalized.quantize(Decimal("0.01"))


@router.put("/api/admin/grocery-prices/ingredient/{slug}")
async def update_ingredient_price_manual(
    slug: str,
    body: IngredientPriceUpdate,
    request: Request,
    _auth=Depends(require_service_token)
):
    """Preço manual por ingrediente, na unidade canônica dele ou via embalagem comercial."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")

    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                try:
                    ingredient = await resolve_ingredient(conn, slug)
                except ValueError as exc:
                    raise HTTPException(422, str(exc))
                if body.price_unit and body.price_unit != ingredient["unit"]:
                    raise HTTPException(422, f"Ingrediente medido em {ingredient['unit']}; corrija a unidade e o preço.")
                try:
                    sale_price, sale_size, sale_unit = _sale_triplet(body)
                    normalized = _normalized_price(body, ingredient, sale_price, sale_size, sale_unit)
                except ValueError as exc:
                    raise HTTPException(422, str(exc))
                await conn.execute(_MANUAL_INGREDIENT_UPSERT, ingredient["name"], slug,
                                   normalized, ingredient["unit"], sale_price, sale_size, sale_unit)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Erro ao atualizar preço manual.")
    return {"success": True, "slug": ingredient["slug"], "food_name": ingredient["name"],
            "price": body.price, "unit": ingredient["unit"]}


async def _parse_price_rows(raw, content_type):
    """Extrai linhas {referencia, price, price_unit} de CSV UTF-8 ou lista JSON."""
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "Importação limitada a 1 MiB.")
    try:
        text = raw.decode("utf-8-sig")
        if content_type == "application/json":
            rows = json.loads(text)
            if not isinstance(rows, list):
                raise ValueError("Esperada uma lista JSON")
        else:
            reference_columns = (["slug", "price"], ["food_name", "price"], ["price", "slug"],
                                 ["price", "food_name"], ["slug", "price", "price_unit"],
                                 ["slug", "price", "sale_price", "sale_size", "sale_unit"],
                                 ["food_name", "price", "sale_price", "sale_size", "sale_unit"])
            delimiter = next((sep for sep in (",", ";")
                              if next(csv.reader(io.StringIO(text), delimiter=sep), []) in reference_columns), None)
            if delimiter is None:
                raise ValueError("Cabeçalho esperado: slug,price (com sale_price,sale_size,sale_unit opcionais)")
            rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True))
        if not rows or len(rows) > 5000:
            raise ValueError("Informe entre 1 e 5000 linhas")
    except (ValueError, UnicodeError, csv.Error):
        raise HTTPException(422, "CSV ou JSON inválido. Use slug e price.")
    return rows


@router.post("/api/admin/grocery-prices/import-ingredients")
async def import_ingredient_prices(request: Request, _auth=Depends(require_service_token)):
    """Importa preços manuais em lote por ingrediente (slug ou nome), na unidade do catálogo."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Importação limitada a 1 MiB.")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    rows = await _parse_price_rows(bytes(raw), content_type)

    valid, details, seen = [], [], set()
    try:
        async with _state.db_pool.acquire() as conn:
            ingredients = await load_ingredients(conn)
            catalog = {key(item["slug"]): item for item in ingredients}
            catalog.update({key(item["name"]): item for item in ingredients})
            for index, row in enumerate(rows, 1):
                try:
                    if not isinstance(row, dict):
                        raise ValueError("Linha inválida")
                    reference = row.get("slug") or row.get("food_name")
                    if not isinstance(reference, str) or not reference.strip():
                        raise ValueError("Informe o slug ou o nome do ingrediente")
                    ingredient = catalog.get(key(reference.strip()))
                    if ingredient is None:
                        raise ValueError("Ingrediente fora do catálogo ativo")
                    if ingredient["slug"] in seen:
                        raise ValueError("Ingrediente repetido")
                    value = row.get("price")
                    if isinstance(value, bool) or row.get(None) is not None:
                        raise ValueError("Preço ou colunas inválidos")
                    if isinstance(value, str):
                        value = value.strip().replace(",", ".")
                    model = ManualPriceUpdate(price=value, price_unit=row.get("price_unit"))
                    if model.price_unit and model.price_unit != ingredient["unit"]:
                        raise ValueError(f"Unidade do catálogo é {ingredient['unit']}")
                    sale_price, sale_size, sale_unit = _import_sale_triplet(row, ingredient)
                    if sale_unit is None:
                        normalized = Decimal(str(model.price))
                    else:
                        normalized = _normalized_price(model, ingredient, sale_price, sale_size, sale_unit)
                    valid.append((ingredient["name"], ingredient["slug"],
                                  normalized, ingredient["unit"], sale_price, sale_size, sale_unit))
                    seen.add(ingredient["slug"])
                except (ValueError, TypeError):
                    details.append({"linha": index, "erro": "Ingrediente, preço ou unidade inválidos."})
        if valid:
            async with _state.db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(_MANUAL_INGREDIENT_UPSERT, valid)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Falha ao importar preços; nenhuma linha do lote foi gravada.")
    print(f"[ADMIN-CATALOG] Importação ingredientes: importados={len(valid)} erros={len(details)}")
    return {"importados": len(valid), "erros": len(details), "detalhes": details}


# ---- CRUD do catálogo de ingredientes (webservice) ------------------------

def _crud_error(exc: Exception) -> HTTPException:
    """Erro de CRUD em mensagem segura para o painel, sem vazar detalhe interno."""
    if isinstance(exc, ForeignKeyViolationError):
        return HTTPException(409, "Ingrediente ainda vinculado a outros registros. Desative-o em vez de excluir.")
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "Falha na operação sobre o catálogo.")


METADATA_COLUMNS = {"slug", "name", "unit", "active", "minimum", "step",
                    "grams_per_unit", "group", "aliases", "food_ids"}
EXPORT_PRICE_COLUMNS = {"source", "price_avg", "sale_price", "sale_size", "sale_unit"}


def _parse_metadata_rows(raw, content_type):
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "Importação limitada a 1 MiB.")
    try:
        text = raw.decode("utf-8-sig")
        if content_type == "application/json":
            rows = json.loads(text)
        else:
            delimiter = next((sep for sep in (";", ",")
                              if "slug" in next(csv.reader(io.StringIO(text), delimiter=sep), [])), None)
            if delimiter is None:
                raise ValueError("Cabeçalho precisa de slug.")
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not set(fields) <= METADATA_COLUMNS | EXPORT_PRICE_COLUMNS:
                raise ValueError("Cabeçalho contém colunas desconhecidas ou repetidas.")
            rows = list(reader)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
            raise ValueError("Informe entre 1 e 5000 linhas.")
        result, seen = [], set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not set(row) <= METADATA_COLUMNS | EXPORT_PRICE_COLUMNS:
                raise ValueError(f"Linha {index}: colunas inválidas.")
            slug = row.get("slug")
            if not isinstance(slug, str) or not slug.strip() or slug.strip() in seen:
                raise ValueError(f"Linha {index}: slug vazio ou repetido.")
            slug = slug.strip()
            seen.add(slug)
            data = {k: v for k, v in row.items() if k in METADATA_COLUMNS - {"slug"} and v != ""}
            for field in ("minimum", "step", "grams_per_unit"):
                if isinstance(data.get(field), str):
                    data[field] = float(data[field].replace(",", "."))
            if isinstance(data.get("active"), str):
                active = data["active"].strip().lower()
                if active not in ("true", "false", "1", "0"):
                    raise ValueError(f"Linha {index}: active deve ser true ou false.")
                data["active"] = active in ("true", "1")
            for field in ("aliases", "food_ids"):
                if isinstance(data.get(field), str):
                    data[field] = json.loads(data[field])
            try:
                ingredient_admin.validate_payload(data, partial=True)
            except ValueError as exc:
                raise ValueError(f"Linha {index}: {exc}") from exc
            result.append((slug, data))
        return result
    except (ValueError, TypeError, UnicodeError, csv.Error) as exc:
        raise HTTPException(422, f"CSV ou JSON de metadados inválido: {exc}") from exc


@router.post("/api/admin/grocery-ingredients/import")
async def import_grocery_ingredients(request: Request, _auth=Depends(require_service_token)):
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Importação limitada a 1 MiB.")
    rows = _parse_metadata_rows(bytes(raw), request.headers.get("content-type", "").split(";", 1)[0].lower())
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                for index, (slug, data) in enumerate(rows, 1):
                    try:
                        await ingredient_admin.update_ingredient(conn, slug, data, bump=False)
                    except (ValueError, LookupError) as exc:
                        raise ValueError(f"Linha {index}: {exc}") from exc
                revision = await ingredient_admin.bump_revision(conn)
        return {"importados": len(rows), "erros": 0, "detalhes": [], "revision": revision}
    except Exception as exc:
        raise _crud_error(exc) from exc


@router.post("/api/admin/grocery-ingredients")
async def create_grocery_ingredient(request: Request, _auth=Depends(require_service_token)):
    """Cria um ingrediente novo no catálogo da lista de compras."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(422, "Corpo JSON inválido.")
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                item = await ingredient_admin.create_ingredient(conn, data)
        return {"success": True, "item": item}
    except HTTPException:
        raise
    except Exception as exc:
        raise _crud_error(exc)


@router.patch("/api/admin/grocery-ingredients/{slug}")
async def update_grocery_ingredient(slug: str, request: Request, _auth=Depends(require_service_token)):
    """Edita nome, unidade, metadados ou situação ativo/inativo de um ingrediente."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(422, "Corpo JSON inválido.")
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                item = await ingredient_admin.update_ingredient(conn, slug, data)
        return {"success": True, "item": item}
    except HTTPException:
        raise
    except Exception as exc:
        raise _crud_error(exc)


@router.delete("/api/admin/grocery-ingredients/{slug}")
async def delete_grocery_ingredient(slug: str, request: Request, _auth=Depends(require_service_token)):
    """Excluí definitivamente; bloqueado quando há contribuições de usuários."""
    if not _state.db_pool:
        raise HTTPException(503, "Banco de dados indisponível.")
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                item = await ingredient_admin.delete_ingredient(conn, slug)
        return {"success": True, "item": item}
    except HTTPException:
        raise
    except Exception as exc:
        raise _crud_error(exc)


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


# Retail packages are separate from canonical ingredient prices.


@router.get("/api/catalog/grocery/cards", response_model=list[GroceryRetailCardOut])
async def list_grocery_retail_cards(category: Optional[str] = None, q: Optional[str] = None):
    if not _state.db_pool:
        raise HTTPException(503, "Catálogo de compras indisponível.")
    async with _state.db_pool.acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            return await load_cards(conn, category, q)


@router.post("/api/catalog/grocery/basket-comparison", response_model=BasketComparisonResult)
async def compare_grocery_basket(payload: BasketComparisonRequest):
    if not _state.db_pool:
        raise HTTPException(503, "Comparação indisponível.")
    async with _state.db_pool.acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            cards = await load_cards(conn)
            markets = await conn.fetch("SELECT * FROM grocery_markets WHERE active ORDER BY id")
    try:
        return compare_basket(payload.items, cards, markets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/admin/grocery/retail")
async def get_admin_grocery_retail(_auth=Depends(require_service_token)):
    if not _state.db_pool:
        raise HTTPException(503, "Catálogo de compras indisponível.")
    async with _state.db_pool.acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            cards = await load_cards(conn, include_inactive=True)
            markets = await conn.fetch("SELECT * FROM grocery_markets ORDER BY name,id")
            ingredients = await conn.fetch("SELECT slug,name,unit FROM grocery_ingredients ORDER BY name")
    return {"cards": cards, "markets": [dict(m) for m in markets],
            "ingredients": [dict(i) for i in ingredients]}


@router.put("/api/admin/grocery/retail")
async def put_admin_grocery_retail(body: GroceryAdminMutation, _auth=Depends(require_service_token)):
    if not _state.db_pool:
        raise HTTPException(503, "Catálogo de compras indisponível.")
    try:
        async with _state.db_pool.acquire() as conn:
            async with conn.transaction():
                await save_record(conn, body.kind, body.payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ForeignKeyViolationError as exc:
        raise HTTPException(422, "Produto, mercado ou ingrediente não encontrado. Atualize o catálogo.") from exc
    return {"success": True}


from routers.grocery_editorial import build_router as build_editorial_router, build_public_router
router.include_router(build_editorial_router(lambda: _state.db_pool, require_service_token))

router.include_router(build_public_router(lambda: _state.db_pool))
