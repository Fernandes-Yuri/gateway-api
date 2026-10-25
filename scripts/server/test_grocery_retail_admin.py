from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import catalog
from services.grocery_retail_admin import save_record


def product(**values):
    return dict(id='butter-200g', name='Manteiga', category='Laticínios',
        image_url='https://example.org/image.png', package_display='Pote 200g',
        package_net_value=200, package_unit='g', kcal_per_package=1400, price_avg='12.90',
        linked_ingredient_slug='butter', **values)


@pytest.fixture
def admin_client(monkeypatch):
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    catalog.configure_state(db_pool=pool)
    monkeypatch.setattr(catalog, '_service_token', lambda: 'only-a-test-value')
    app = FastAPI()
    app.include_router(catalog.router)
    yield TestClient(app), conn
    catalog.configure_state()


AUTH = {'Authorization': 'Bearer only-a-test-value'}


def test_editorial_endpoints_require_existing_admin_authorization(admin_client):
    http, conn = admin_client
    assert http.get('/api/admin/grocery/retail').status_code == 401
    assert http.put('/api/admin/grocery/retail', json={'kind': 'market', 'payload': {}}).status_code == 401
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()


def test_admin_snapshot_includes_inactive_records_and_ingredient_choices(admin_client):
    http, conn = admin_client
    conn.fetch.side_effect = [[], [], [dict(id='x', name='X', active=False)], [dict(slug='butter', name='Manteiga', unit='kg')]]
    response = http.get('/api/admin/grocery/retail?include_inactive=true', headers=AUTH)
    assert response.status_code == 200
    assert response.json()['markets'][0]['active'] is False
    assert response.json()['ingredients'][0]['slug'] == 'butter'
    assert 'WHERE TRUE' in conn.fetch.call_args_list[0].args[0]
    assert 'WHERE TRUE' in conn.fetch.call_args_list[1].args[0]


def test_admin_snapshot_defaults_to_active_records(admin_client):
    http, conn = admin_client
    conn.fetch.side_effect = [[], [], [dict(id='x', name='X', active=True)], [dict(slug='butter', name='Manteiga', unit='kg')]]
    response = http.get('/api/admin/grocery/retail', headers=AUTH)
    assert response.status_code == 200
    assert 'WHERE active' in conn.fetch.call_args_list[0].args[0]
    assert 'WHERE active = TRUE' in conn.fetch.call_args_list[2].args[0]


async def test_saves_photo_packaging_nutrition_and_ingredient_without_kg_rounding():
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    await save_record(conn, 'card', product())
    args = conn.execute.call_args.args
    assert '200' not in args[0]  # Values must be bound, never interpolated.
    assert Decimal('200') in args[1:]
    assert Decimal('1400') in args[1:]
    assert Decimal('12.90') in args[1:]
    assert 'https://example.org/image.png' in args[1:]
    assert 'Pote 200g' in args[1:]
    assert 'updated_at=now()' in args[0]


async def test_incompatible_pantry_unit_rejected_before_mutation():
    conn = AsyncMock()
    conn.fetchval.return_value = 'L'
    with pytest.raises(ValueError, match='unidade da embalagem'):
        await save_record(conn, 'card', product())
    conn.execute.assert_not_awaited()


async def test_quote_preserves_price_research_source_and_timestamp():
    conn = AsyncMock()
    date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await save_record(conn, 'quote', dict(card_id='a', market_id='b', price='8.90',
        product_store_url='https://example.org/produto', last_updated=date.isoformat()))
    args = conn.execute.call_args.args
    assert date in args and Decimal('8.90') in args
    assert 'https://example.org/produto' in args
    assert 'now()' not in args[0]


async def test_remove_quote_is_scoped_to_both_card_and_market():
    conn = AsyncMock()
    await save_record(conn, 'remove_quote', dict(card_id='a', market_id='b'))
    conn.execute.assert_awaited_once_with('DELETE FROM grocery_market_prices WHERE card_id=$1 AND market_id=$2', 'a', 'b')


async def test_composite_card_update_saves_pantry_fields_and_product_together(monkeypatch):
    from services import grocery_retail_admin
    update = AsyncMock()
    monkeypatch.setattr(grocery_retail_admin, 'update_ingredient', update)
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    nutrition = [
        {'key': 'calories', 'label': 'Calorias', 'unit': 'kcal', 'value': 1400},
        {'key': 'fiber', 'label': 'Fibras alimentares', 'unit': 'g', 'value': 12},
        {'key': 'sodium', 'label': 'Sódio', 'unit': 'mg', 'value': 20},
    ]
    await grocery_retail_admin.save_record(conn, 'card_with_ingredient', {
        'card': product(nutrition_facts=nutrition),
        'ingredient': {'slug': 'butter', 'changes': {'unit': 'kg', 'group': 'laticinios',
            'minimum': 100, 'step': 50, 'grams_per_unit': None, 'aliases': ['manteiga sem sal'], 'active': True}},
    })
    update.assert_awaited_once_with(conn, 'butter', {'unit': 'kg', 'group': 'laticinios',
        'minimum': 100, 'step': 50, 'grams_per_unit': None, 'aliases': ['manteiga sem sal'], 'active': True})
    execute_args = conn.execute.call_args.args
    assert 'nutrition_facts' in execute_args[0]
    assert any('sodium' in str(value) for value in execute_args)


async def test_composite_card_update_rejects_mismatched_ingredient_before_write():
    conn = AsyncMock()
    with pytest.raises(ValueError, match='mesmo alimento'):
        await save_record(conn, 'card_with_ingredient', {
            'card': product(), 'ingredient': {'slug': 'other', 'changes': {'unit': 'kg'}}})
    conn.execute.assert_not_awaited()


def test_admin_validation_returns_422_without_writing(admin_client):
    http, conn = admin_client
    response = http.put('/api/admin/grocery/retail', headers=AUTH, json={'kind': 'quote', 'payload': {'price': -1}})
    assert response.status_code == 422
    conn.execute.assert_not_awaited()


async def test_market_retirement_keeps_quotes_and_only_updates_editorial_record():
    conn = AsyncMock()
    await save_record(conn, 'market', dict(id='a', name='Mercado A', logo_url='https://example.org/logo.png',
        website_url='https://example.org', active=False))
    args = conn.execute.call_args.args
    assert args[0].startswith('INSERT INTO grocery_markets')
    assert False in args[1:]
    assert 'DELETE' not in args[0]


@pytest.mark.parametrize('fiber', [None, 0, '12.50'])
def test_dietary_fiber_accepts_unknown_zero_and_measured_values(fiber):
    from schemas.grocery_cards import GroceryRetailCardIn
    item = GroceryRetailCardIn(**product(fiber_g=fiber))
    assert item.fiber_g is None if fiber is None else item.fiber_g == Decimal(str(fiber))


@pytest.mark.parametrize('fiber', [-1, '1.234', 'NaN', 'Infinity'])
async def test_dietary_fiber_rejects_invalid_values_without_writing(fiber):
    conn = AsyncMock()
    with pytest.raises(ValueError, match='fibras alimentares'):
        await save_record(conn, 'card', product(fiber_g=fiber))
    conn.execute.assert_not_awaited()


async def test_food_rejects_future_reference_before_creating_records():
    from datetime import timedelta
    conn = AsyncMock()
    with pytest.raises(ValueError, match='futuro'):
        await save_record(conn, 'food', {'card': product(), 'reference': {
            'market_id': 'fonte', 'last_updated': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}})
    conn.execute.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


async def test_food_reference_can_preserve_a_separate_observed_price_and_product_url(monkeypatch):
    from services import grocery_retail_admin
    monkeypatch.setattr(grocery_retail_admin, 'update_ingredient', AsyncMock())
    conn = AsyncMock()
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    conn.fetchval.return_value = 'kg'
    date = '2026-01-15T12:00:00+00:00'
    await save_record(conn, 'food', {'card': product(), 'reference': {
        'market_id': 'fonte', 'price': 10.50, 'last_updated': date,
        'product_store_url': 'https://example.org/manteiga'}})
    card_args = conn.execute.call_args_list[-2].args
    quote_args = conn.execute.call_args_list[-1].args
    assert Decimal('12.90') in card_args
    assert Decimal('10.50') in quote_args
    assert datetime.fromisoformat(date) in quote_args
    assert 'https://example.org/manteiga' in quote_args


async def test_food_rejects_incompatible_dimension_before_changing_the_food():
    conn = AsyncMock()
    conn.fetchrow.return_value = {'linked_ingredient_slug': 'butter'}
    conn.fetchval.return_value = 'L'
    with pytest.raises(ValueError, match='Peso e volume'):
        await save_record(conn, 'food', {'card': product()})
    conn.execute.assert_not_awaited()


async def test_nutrient_suggestions_require_admin_and_do_not_mutate_catalog(admin_client):
    http, conn = admin_client
    path = '/api/admin/grocery/nutrition-suggestions?q=%C3%93leo%20de%20Soja&amount=900&unit=ml'
    assert http.get(path).status_code == 401
    result = http.get(path, headers={'Authorization': 'Bearer only-a-test-value'})
    assert result.status_code == 200
    first = result.json()['candidates'][0]
    assert first['id'] == 'TACO:272'
    assert first['conversion']['total_grams'] == 828
    conn.execute.assert_not_awaited()
    assert http.get('/api/admin/grocery/nutrition-suggestions?q=oleo&amount=nan&unit=ml',
                    headers={'Authorization': 'Bearer only-a-test-value'}).status_code == 422


async def test_reviewed_composition_source_is_preserved_with_nutrients():
    import json
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    reference = {'food_id': 'TACO:272', 'source_name': 'TACO / NEPA-UNICAMP', 'basis_g': 100,
                 'package_mass_g': 100, 'suggested_value': 100, 'reviewed_at': '2026-10-18T00:00:00Z'}
    card = product()
    card['nutrition_facts'] = [{'key': 'fat', 'label': 'Gorduras', 'unit': 'g', 'value': 100, 'reference': reference}]
    await save_record(conn, 'card', card)
    args = conn.execute.call_args.args
    stored = next(json.loads(value) for value in args[1:] if isinstance(value, str) and value.startswith('[{"key"'))
    assert stored[0]['reference'] == reference


async def test_image_credit_and_url_saved_and_bound():
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    card = product(
        image_credit='Autor Exemplo / CC-BY-SA 4.0',
        image_credit_url='https://example.org/foto-autor',
    )
    await save_record(conn, 'card', card)
    args = conn.execute.call_args.args
    assert 'image_credit' in args[0]
    assert 'image_credit_url' in args[0]
    assert 'Autor Exemplo / CC-BY-SA 4.0' in args[1:]
    assert 'https://example.org/foto-autor' in args[1:]


async def test_whitespace_image_credit_normalized_to_none_for_kill_switch():
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    card = product(image_credit='   ', image_credit_url='   ')
    await save_record(conn, 'card', card)
    args = conn.execute.call_args.args
    columns = [col.strip() for col in args[0].split('(')[1].split(')')[0].split(',')]
    credit_idx = columns.index('image_credit') + 1
    credit_url_idx = columns.index('image_credit_url') + 1
    assert args[credit_idx] is None
    assert args[credit_url_idx] is None


async def test_invalid_image_credit_url_rejected_before_write():
    conn = AsyncMock()
    conn.fetchval.return_value = 'kg'
    conn.fetchrow.return_value = {'name': 'Manteiga', 'linked_ingredient_slug': 'butter'}
    with pytest.raises(ValueError, match='crédito da imagem'):
        await save_record(conn, 'card', product(image_credit_url='javascript:alert(1)'))
    conn.execute.assert_not_awaited()
