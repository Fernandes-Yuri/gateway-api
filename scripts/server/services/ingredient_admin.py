"""Gestão administrativa do catálogo de ingredientes: criar, editar, desativar e excluir.

Regras de segurança do CRUD:
- desativar (active=false) é o caminho padrão: reversível e preserva histórico;
- excluir é guardada: só remove ingrediente sem contribuições de usuários;
- editar preserva os metadados JSONB não tocados (nada é apagado por acidente);
- qualquer alteração sobe a revisão do catálogo, para o app baixar de novo;
- unidade é sempre kg, L ou un, a mesma aceita pelo schema do banco.
"""
import json
import re
import unicodedata

from services.ingredient_catalog import as_json, key, load_ingredients

ALLOWED_UNITS = ("kg", "L", "un")
MAX_NAME = 200
MAX_ALIAS = 100
MAX_ALIASES = 20
DETAIL_FIELDS = ("aliases", "food_ids", "grams_per_unit", "minimum", "step")


FORBIDDEN_NAME_PATTERNS = [
    (re.compile(r"\bcozid[oa]s?\b", re.IGNORECASE), "Cozido/Cozida"),
    (re.compile(r"\brefogad[oa]s?\b", re.IGNORECASE), "Refogado/Refogada"),
    (re.compile(r"\bgrelhad[oa]s?\b", re.IGNORECASE), "Grelhado/Grelhada"),
    (re.compile(r"\bassad[oa]s?\b", re.IGNORECASE), "Assado/Assada"),
    (re.compile(r"\bno vapor\b", re.IGNORECASE), "No Vapor"),
    (re.compile(r"\btostad[oa]s?\b", re.IGNORECASE), "Tostado/Tostada"),
    (re.compile(r"\bdesfiad[oa]s?\b", re.IGNORECASE), "Desfiado/Desfiada"),
    (re.compile(r"\bcru[as]?\b", re.IGNORECASE), "Cru/Crua"),
    (re.compile(r"\bfatiad[oa]s?\b", re.IGNORECASE), "Fatiado/Fatiada"),
    (re.compile(r"\bem cubos\b", re.IGNORECASE), "em Cubos"),
    (re.compile(r"\bpicad[oa]s?\b", re.IGNORECASE), "Picado/Picada"),
    (re.compile(r"\bmo[ií]d[oa]s?\b", re.IGNORECASE), "Moído/Moída"),
    (re.compile(r"\bpoch[eê]\b", re.IGNORECASE), "Pochê"),
    (re.compile(r"\bomelete\b", re.IGNORECASE), "Omelete"),
    (re.compile(r"\bmexid[oa]s?\b", re.IGNORECASE), "Mexido/Mexida"),
    (re.compile(r"\bao molho\b", re.IGNORECASE), "ao Molho"),
]


def slugify(name):
    """Slug estável: 'Pão de Queijo Tradicional' -> 'pao-de-queijo-tradicional'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:80].strip("-")


def _clean_text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} precisa de texto não vazio com até {limit} caracteres.")
    return value.strip()


def _validate_clean_name(value):
    cleaned = _clean_text(value, "Nome", MAX_NAME)
    for pattern, label in FORBIDDEN_NAME_PATTERNS:
        if pattern.search(cleaned):
            raise ValueError(
                f"Nome '{cleaned}' inválido: contém termo de preparo ou corte ('{label}'). "
                f"Ingredientes de compras devem usar o nome comercial simples do produto (ex.: 'Beterraba', 'Peito de Frango'). "
                f"Detalhes de cocção pertencem aos preparos e sinônimos em aliases."
            )
    return cleaned


def _clean_aliases(aliases, ingredient_name=None):
    if aliases is None:
        return []
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        raise ValueError("Sinônimos devem ser uma lista com no máximo 20 itens.")
    cleaned, seen = [], set()
    name_key = key(ingredient_name) if ingredient_name else None
    for alias in aliases:
        alias = _clean_text(alias, "Sinônimo", MAX_ALIAS)
        ak = key(alias)
        if ak != name_key and ak not in seen:
            cleaned.append(alias)
            seen.add(ak)
    return cleaned


def _clean_number(value, field, limit):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= limit:
        raise ValueError(f"{field} deve ser um número positivo até {limit}.")
    return float(value)


def _clean_food_ids(food_ids):
    if food_ids is None:
        return None
    if not isinstance(food_ids, list) or len(food_ids) > 50:
        raise ValueError("Alimentos ligados devem ser uma lista com no máximo 50 itens.")
    cleaned, seen = [], set()
    for food_id in food_ids:
        food_id = _clean_text(food_id, "Identificador de alimento", 40)
        if food_id not in seen:
            cleaned.append(food_id)
            seen.add(food_id)
    return cleaned


def validate_payload(data, *, partial=False):
    """Valida o corpo de criação/edição e devolve apenas os campos aceitos."""
    if not isinstance(data, dict):
        raise ValueError("Envie um objeto com os campos do ingrediente.")
    allowed = {"name", "unit", "aliases", "grams_per_unit", "minimum", "step", "group", "food_ids", "active"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Campos desconhecidos: " + ", ".join(sorted(unknown)))
    if partial and not data:
        raise ValueError("Informe ao menos um campo para editar.")
    result = {}
    if "name" in data:
        result["name"] = _validate_clean_name(data["name"])
    if "unit" in data:
        if data["unit"] not in ALLOWED_UNITS:
            raise ValueError("Unidade deve ser kg, L ou un.")
        result["unit"] = data["unit"]
    if "aliases" in data:
        result["aliases"] = _clean_aliases(data["aliases"], result.get("name") or data.get("name"))
    if "grams_per_unit" in data:
        result["grams_per_unit"] = _clean_number(data["grams_per_unit"], "Gramas por unidade", 100000)
    if "minimum" in data:
        result["minimum"] = _clean_number(data["minimum"], "Quantidade mínima", 100000)
    if "step" in data:
        value = _clean_number(data["step"], "Passo de compra", 100000)
        if value is None or value < 1 or not value.is_integer():
            raise ValueError("Passo de compra deve ser um inteiro entre 1 e 100000.")
        result["step"] = int(value)
    if "group" in data:
        result["group"] = _clean_text(data["group"], "Grupo", 80) if data["group"] else None
    if "food_ids" in data:
        result["food_ids"] = _clean_food_ids(data["food_ids"])
    if "active" in data:
        if not isinstance(data["active"], bool):
            raise ValueError("Situação ativo/inativo deve ser verdadeiro ou falso.")
        result["active"] = data["active"]
    if not partial and not {"name", "unit"} <= set(result):
        raise ValueError("Nome e unidade são obrigatórios para criar um ingrediente.")
    return result


def build_details(payload, current_details):
    """Detalhes JSONB a gravar: preserva o que não foi tocado e aplica o novo."""
    details = dict(current_details or {})
    for field in DETAIL_FIELDS:
        if field in payload:
            details[field] = payload[field]
    if "group" in payload:
        details.pop("canonical_group_slug", None)
        if payload["group"]:
            details["canonical_group_slug"] = payload["group"]
    return {field: value for field, value in details.items() if value is not None}


async def _taken_names(conn, slug=None):
    """Nomes e sinônimos já usados por outros ingredientes (para colisão)."""
    collisions = set()
    for item in await load_ingredients(conn, active_only=False):
        if item["slug"] == slug:
            continue
        collisions.update(key(name) for name in [item["name"], *item.get("aliases", [])])
    return collisions


def check_name_free(name, collisions):
    if key(name) in collisions:
        raise ValueError("Já existe um ingrediente com esse nome ou sinônimo.")


def pick_slug(name, taken):
    """Slug derivado do nome, com sufixo numérico quando já estiver em uso."""
    base = slugify(name) or "ingrediente"
    slug, suffix = base, 2
    while slug in taken:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


async def existing_slugs(conn):
    return {item["slug"] for item in await load_ingredients(conn, active_only=False)}


async def bump_revision(conn):
    """Incrementa a revisão do catálogo para o app sincronizar de novo."""
    return await conn.fetchval("UPDATE nutrition_catalog_settings SET revision = revision + 1 WHERE id = 1 RETURNING revision")


async def _check_food_ids(conn, food_ids):
    if not food_ids:
        return
    known = {row["remote_id"] for row in await conn.fetch("SELECT remote_id FROM food_catalog")}
    unknown = [food_id for food_id in food_ids if food_id not in known]
    if unknown:
        raise ValueError("Alimentos fora do catálogo: " + ", ".join(unknown))


async def _load_row(conn, slug):
    row = await conn.fetchrow("SELECT slug,name,unit,details,active FROM grocery_ingredients WHERE slug=$1 FOR UPDATE", slug)
    if row is None:
        raise LookupError("Ingrediente não encontrado.")
    return row


async def create_ingredient(conn, data):
    payload = validate_payload(data)
    check_name_free(payload["name"], await _taken_names(conn))
    await _check_food_ids(conn, payload.get("food_ids"))
    slug = pick_slug(payload["name"], await existing_slugs(conn))
    details = build_details(payload, {})
    await conn.execute(
        """INSERT INTO grocery_ingredients (slug, name, unit, details, active)
           VALUES ($1, $2, $3, $4::jsonb, TRUE)""",
        slug, payload["name"], payload["unit"], _as_json(details))
    revision = await bump_revision(conn)
    print(f"[ADMIN-CATALOG] Ingrediente criado: {slug} ({payload['name']}) unidade={payload['unit']}")
    return {"slug": slug, "name": payload["name"], "unit": payload["unit"], "active": True,
            "revision": revision, "details": details, "avisos": []}


async def update_ingredient(conn, slug, data, *, bump=True):
    payload = validate_payload(data, partial=True)
    current = await _load_row(conn, slug)
    current_details = as_json(current["details"]) or {}
    if "name" in payload:
        check_name_free(payload["name"], await _taken_names(conn, slug=slug))
    await _check_food_ids(conn, payload.get("food_ids"))
    details = build_details(payload, current_details)
    name = payload.get("name", current["name"])
    unit = payload.get("unit", current["unit"])
    active = payload.get("active", bool(current["active"]))
    avisos = []
    if "unit" in payload and unit != current["unit"]:
        avisos.append(f"Unidade alterada de {current['unit']} para {unit}: preços manuais e médias da comunidade ligados à unidade antiga deixam de valer e o item volta a ficar pendente.")
    if "name" in payload and name != current["name"]:
        # Bind old name-based prices to the stable ingredient before renaming.
        # Values, packaging and research dates stay untouched.
        await conn.execute("""UPDATE grocery_price_cache SET ingredient_slug=$1
            WHERE ingredient_slug IS NULL AND food_name=$2 AND price_unit=$3""",
            slug, current["name"], current["unit"])
    if not active and current["active"]:
        avisos.append("Ingrediente desativado: some da lista de compras e do app; reative quando quiser.")
    await conn.execute(
        "UPDATE grocery_ingredients SET name=$2, unit=$3, details=$4::jsonb, active=$5 WHERE slug=$1",
        slug, name, unit, _as_json(details), active)
    if "name" in payload:
        await conn.execute("UPDATE grocery_retail_cards SET name=$2,updated_at=now() WHERE linked_ingredient_slug=$1 AND name IS DISTINCT FROM $2",
                           slug, name)
    revision = await bump_revision(conn) if bump else None
    print(f"[ADMIN-CATALOG] Ingrediente editado: {slug} ({name}) campos={sorted(payload)}")
    return {"slug": slug, "name": name, "unit": unit, "active": active, "revision": revision,
            "details": details, "avisos": avisos}


async def delete_ingredient(conn, slug):
    """Exclusão dura, bloqueada quando o ingrediente já tem contribuições."""
    current = await _load_row(conn, slug)
    total = await conn.fetchval("SELECT COUNT(*) FROM grocery_price_contributions WHERE ingredient_slug=$1", slug)
    if total:
        raise PermissionError(f"Ingrediente com {total} contribuição(ões) de usuários. Desative-o em vez de excluir, para preservar o histórico.")
    await conn.execute("""INSERT INTO grocery_price_cache_history
        (food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit, reason)
        SELECT food_name, ingredient_slug, price_avg, price_min, price_max, source, price_unit,
               'ingredient_deleted'
        FROM grocery_price_cache WHERE ingredient_slug=$1""", slug)
    await conn.execute("DELETE FROM grocery_price_cache WHERE ingredient_slug=$1", slug)
    await conn.execute("DELETE FROM grocery_ingredients WHERE slug=$1", slug)
    revision = await bump_revision(conn)
    print(f"[ADMIN-CATALOG] Ingrediente excluído: {slug} ({current['name']})")
    return {"slug": slug, "name": current["name"], "revision": revision}


def _as_json(details):
    return json.dumps(details, ensure_ascii=False, separators=(",", ":"))
