"""Editorial mutations used by the webservice. Values are always bound parameters."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pydantic import ValidationError
from schemas.grocery_cards import GroceryRetailCardIn, MarketIn, PriceIn
from services.ingredient_admin import update_ingredient


async def save_record(conn, kind, payload):
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
        data['nutrition_facts'] = json.dumps(facts)
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
    for field in ('image_url', 'logo_url', 'website_url', 'product_store_url'):
        if data.get(field) is not None:
            data[field] = str(data[field])
    for field in ('image_url', 'logo_url'):
        if data.get(field) and not data[field].startswith('https://'):
            raise ValueError('Use uma URL HTTPS pública para a imagem ou o logotipo.')
    columns = list(data)
    # Table and columns come only from the fixed mapping and validated model.
    await conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES (" +
        ','.join(f'${i}' for i in range(1, len(columns)+1)) + ') ON CONFLICT (' + ','.join(keys) +
        ') DO UPDATE SET ' + ','.join(f'{c}=EXCLUDED.{c}' for c in columns if c not in keys),
        *(data[c] for c in columns))
