"""Editorial mutations used by the webservice. Values are always bound parameters."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pydantic import ValidationError
from schemas.grocery_cards import GroceryRetailCardIn, MarketIn, PriceIn
from services.ingredient_admin import update_ingredient, create_ingredient
from services.ingredient_catalog import key, load_ingredients


async def save_record(conn, kind, payload):
    if kind == 'food':
        await save_food(conn, payload)
        return
    if kind == 'card_with_ingredient':
        if set(payload) != {'card', 'ingredient'} or not isinstance(payload['card'], dict) or not isinstance(payload['ingredient'], dict):
            raise ValueError('Informe os dados do produto e do alimento.')
        card = payload['card']
        ingredient = payload['ingredient']
        slug = ingredient.get('slug')
        changes = ingredient.get('changes')
        if not isinstance(slug, str) or not slug.strip() or not isinstance(changes, dict):
            raise ValueError('Informe o alimento e seus dados de compra.')
        if card.get('linked_ingredient_slug') != slug:
            raise ValueError('O produto e os dados da despensa precisam apontar para o mesmo alimento.')
        allowed = {'unit', 'group', 'minimum', 'step', 'grams_per_unit', 'aliases', 'active'}
        if set(changes) - allowed:
            raise ValueError('Há campos de compra não reconhecidos.')
        await update_ingredient(conn, slug, changes)
        await save_record(conn, 'card', card)
        return
    if kind == 'remove_quote':
        if set(payload) != {'card_id', 'market_id'} or not all(isinstance(v, str) and v.strip() for v in payload.values()):
            raise ValueError('Informe o produto e o mercado da cotação.')
        await conn.execute('DELETE FROM grocery_market_prices WHERE card_id=$1 AND market_id=$2', payload['card_id'], payload['market_id'])
        return
    model, table, keys = {
        'card': (GroceryRetailCardIn, 'grocery_retail_cards', ['id']),
        'market': (MarketIn, 'grocery_markets', ['id']),
        'quote': (PriceIn, 'grocery_market_prices', ['card_id', 'market_id']),
    }[kind]
    try:
        record = model.model_validate(payload)
    except ValidationError as exc:
        labels = {
            'id': 'identificador', 'name': 'nome', 'category': 'categoria',
            'image_url': 'imagem', 'logo_url': 'logotipo', 'website_url': 'site da loja',
            'package_display': 'apresentação da embalagem', 'package_net_value': 'conteúdo da embalagem',
            'package_unit': 'unidade da embalagem', 'kcal_per_package': 'calorias por embalagem',
            'carbs_g': 'carboidratos', 'protein_g': 'proteínas', 'fat_g': 'gorduras', 'fiber_g': 'fibras alimentares',
            'price_avg': 'preço de referência', 'price': 'preço pesquisado',
            'last_updated': 'data da pesquisa', 'product_store_url': 'link do produto',
            'card_id': 'produto', 'market_id': 'supermercado',
            'image_credit': 'crédito da imagem', 'image_credit_url': 'link do crédito da imagem',
        }
        fields = ', '.join(dict.fromkeys(labels.get(str(err['loc'][0]), 'dados do cadastro')
            if err['loc'] else 'dados do cadastro' for err in exc.errors()))
        raise ValueError('Revise os campos: ' + fields) from exc
    data = record.model_dump()
    # JSONB uses an explicit JSON string with asyncpg's default codec.
    if kind == 'card':
        facts = data.get('nutrition_facts') or []
        for fact in facts:
            if (not isinstance(fact, dict) or not isinstance(fact.get('key'), str)
                    or not isinstance(fact.get('label'), str) or not isinstance(fact.get('unit'), str)
                    or not isinstance(fact.get('value'), (int, float, Decimal))):
                raise ValueError('Revise os campos nutricionais.')
            if not fact['key'].strip() or not fact['label'].strip() or not fact['unit'].strip() or Decimal(str(fact['value'])) < 0:
                raise ValueError('Os nutrientes precisam de nome, unidade e valor não negativo.')
        known = {'calories': 'kcal_per_package', 'carbohydrates': 'carbs_g', 'protein': 'protein_g', 'fat': 'fat_g'}
        for key, column in known.items():
            fact = next((item for item in facts if item['key'] == key), None)
            data[column] = Decimal(str(fact['value'])) if fact else Decimal('0')
        fiber = next((item for item in facts if item['key'] == 'fiber'), None)
        data['fiber_g'] = Decimal(str(fiber['value'])) if fiber else None
        data['nutrition_facts'] = json.dumps(facts, default=float)
    if kind in ('card', 'market') and (not data['id'].strip() or not data['name'].strip()):
        raise ValueError('Identificador e nome não podem estar vazios.')
    if kind == 'card':
        for field in ('category', 'package_display'):
            if not data[field].strip():
                raise ValueError('Categoria e apresentação da embalagem são obrigatórias.')
        if data['linked_ingredient_slug']:
            unit = await conn.fetchval('SELECT unit FROM grocery_ingredients WHERE slug=$1 FOR UPDATE', data['linked_ingredient_slug'])
            if unit is None:
                raise ValueError('Ingrediente de despensa não encontrado.')
            if (unit, data['package_unit']) not in {('kg', 'g'), ('L', 'ml'), ('un', 'un')}:
                raise ValueError('A unidade da embalagem deve corresponder ao ingrediente: g/kg, ml/L ou un/un.')
            previous = await conn.fetchrow('SELECT name,linked_ingredient_slug FROM grocery_retail_cards WHERE id=$1', data['id'])
            # A name edit (or explicit new link) updates the ingredient and its
            # sibling packages in the same transaction as the product itself.
            if previous is None or previous['name'] != data['name'] or previous['linked_ingredient_slug'] != data['linked_ingredient_slug']:
                updated = await update_ingredient(conn, data['linked_ingredient_slug'], {'name': data['name']})
                data['name'] = updated['name']

    if kind == 'quote':
        if data['last_updated'] > datetime.now(timezone.utc):
            raise ValueError('A data da pesquisa não pode estar no futuro.')
    for field in ('image_url', 'logo_url', 'website_url', 'product_store_url', 'image_credit_url'):
        if data.get(field) is not None:
            data[field] = str(data[field])
    for field in ('image_url', 'logo_url'):
        if data.get(field) and not data[field].startswith('https://'):
            raise ValueError('Use uma URL HTTPS pública para a imagem ou o logotipo.')
    if data.get('image_credit') is not None:
        credit = str(data['image_credit']).strip()
        data['image_credit'] = credit or None
    if data.get('image_credit_url') is not None:
        credit_url = str(data['image_credit_url']).strip()
        if credit_url and not (credit_url.startswith('https://') or credit_url.startswith('http://')):
            raise ValueError('Use uma URL HTTP ou HTTPS válida para o crédito da imagem.')
        data['image_credit_url'] = credit_url or None
    columns = list(data)
    # Table and columns come only from the fixed mapping and validated model.
    updates = [f'{column}=EXCLUDED.{column}' for column in columns if column not in keys]
    if kind == 'card':
        updates.append('updated_at=now()')
    await conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ("
        + ','.join(f'${i}' for i in range(1, len(columns) + 1))
        + ') ON CONFLICT (' + ','.join(keys) + ') DO UPDATE SET ' + ','.join(updates),
        *(data[column] for column in columns),
    )


async def save_food(conn, payload):
    """One food form: the canonical food and its package are saved in one transaction.

    The route owns the transaction. Preserve recipe links and physical metadata;
    correct the base dimension without treating existing quantities as conversions.
    """
    if set(payload) - {'card', 'reference'} or not isinstance(payload.get('card'), dict):
        raise ValueError('Informe os dados do alimento.')
    card = GroceryRetailCardIn.model_validate(payload['card']).model_dump()
    unit = {'g': 'kg', 'ml': 'L', 'un': 'un'}[card['package_unit']]
    reference = payload.get('reference')
    quote = None
    if reference is not None:
        if (not isinstance(reference, dict) or not {'market_id', 'last_updated'} <= set(reference)
                or set(reference) - {'market_id', 'last_updated', 'price', 'product_store_url', 'product_sku', 'in_stock', 'last_verified_at'}):
            raise ValueError('Informe o mercado de origem e a data da referência.')
        quote = PriceIn.model_validate({'price': card['price_avg'], **reference, 'card_id': card['id']})
        if quote.last_updated > datetime.now(timezone.utc):
            raise ValueError('A data da pesquisa não pode estar no futuro.')
    previous = await conn.fetchrow('SELECT linked_ingredient_slug FROM grocery_retail_cards WHERE id=$1', card['id'])
    slug = card['linked_ingredient_slug'] or (previous['linked_ingredient_slug'] if previous else None)
    if not slug:
        # Exact canonical identity only. No fuzzy matching or repeated manual linking.
        same_name = next((item for item in await load_ingredients(conn, active_only=False)
                          if key(item['name']) == key(card['name'])), None)
        if same_name:
            slug = same_name['slug']
        else:
            created = await create_ingredient(conn, {'name': card['name'], 'unit': unit,
                                                     'group': card['category'], 'minimum': 1, 'step': 1})
            slug = created['slug']
    current_unit = await conn.fetchval('SELECT unit FROM grocery_ingredients WHERE slug=$1 FOR UPDATE', slug)
    if current_unit is None:
        raise ValueError('Alimento não encontrado. Atualize o catálogo.')
    unit_changes = {}
    if current_unit != unit:
        incompatible = await conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM grocery_retail_cards WHERE linked_ingredient_slug=$1 AND id<>$2 AND package_unit<>$3)',
            slug, card['id'], card['package_unit'])
        if incompatible:
            raise ValueError('Este alimento tem outras embalagens em outra unidade. Ajuste essas embalagens antes de mudar a medida. Peso e volume não são convertidos automaticamente.')
        unit_changes = {'unit': unit, 'minimum': 1, 'step': 1}
    # Retiring one package must not retire another active package of the same food.
    has_other_packages = await conn.fetchval(
        'SELECT EXISTS(SELECT 1 FROM grocery_retail_cards WHERE linked_ingredient_slug=$1 AND id<>$2 AND active)',
        slug, card['id'])
    await update_ingredient(conn, slug, {**unit_changes, 'name': card['name'], 'group': card['category'],
                                        'active': card['active'] or bool(has_other_packages)})
    await conn.execute('UPDATE grocery_retail_cards SET category=$2,updated_at=now() WHERE linked_ingredient_slug=$1 AND category IS DISTINCT FROM $2',
                       slug, card['category'])
    card['linked_ingredient_slug'] = slug
    await save_record(conn, 'card', card)
    if quote:
        await save_record(conn, 'quote', quote.model_dump())
