"""Editorial mutations used by the webservice. Values are always bound parameters."""
from datetime import datetime, timezone
from pydantic import ValidationError
from schemas.grocery_cards import GroceryRetailCardIn, MarketIn, PriceIn


async def save_record(conn, kind, payload):
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
            'carbs_g': 'carboidratos', 'protein_g': 'proteínas', 'fat_g': 'gorduras',
            'price_avg': 'preço de referência', 'price': 'preço pesquisado',
            'last_updated': 'data da pesquisa', 'product_store_url': 'link do produto',
            'card_id': 'produto', 'market_id': 'supermercado',
        }
        fields = ', '.join(dict.fromkeys(labels.get(str(err['loc'][0]), 'dados do cadastro')
            if err['loc'] else 'dados do cadastro' for err in exc.errors()))
        raise ValueError('Revise os campos: ' + fields) from exc
    data = record.model_dump()
    if kind in ('card', 'market') and (not data['id'].strip() or not data['name'].strip()):
        raise ValueError('Identificador e nome não podem estar vazios.')
    if kind == 'card':
        for field in ('category', 'package_display'):
            if not data[field].strip():
                raise ValueError('Categoria e apresentação da embalagem são obrigatórias.')
        if data['linked_ingredient_slug']:
            unit = await conn.fetchval('SELECT unit FROM grocery_ingredients WHERE slug=$1', data['linked_ingredient_slug'])
            if unit is None:
                raise ValueError('Ingrediente de despensa não encontrado.')
            if (unit, data['package_unit']) not in {('kg', 'g'), ('L', 'ml'), ('un', 'un')}:
                raise ValueError('A unidade da embalagem deve corresponder ao ingrediente: g/kg, ml/L ou un/un.')
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
