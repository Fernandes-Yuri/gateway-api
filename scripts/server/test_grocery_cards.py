from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from routers import catalog
from seed_grocery_cards import import_sql
from schemas.grocery_cards import GroceryImport


@pytest.fixture
def client():
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    catalog.configure_state(db_pool=pool)
    app = FastAPI()
    app.include_router(catalog.router)
    yield TestClient(app), conn
    catalog.configure_state()


def test_cards_filter_parameters_are_bound(client):
    http, conn = client
    conn.fetch.side_effect = [[], []]
    response = http.get('/api/catalog/grocery/cards', params=dict(category='Laticínios', q="a%_'"))
    assert response.status_code == 200
    assert response.json() == []
    args = conn.fetch.call_args_list[0].args
    assert args[1:] == ('Laticínios', "a%_'")
    assert 'strpos' in args[0]


def test_offline_service_returns_503(client):
    http, _ = client
    catalog.configure_state()
    assert http.get('/api/catalog/grocery/cards').status_code == 503
    assert http.post('/api/catalog/grocery/basket-comparison', json={'items': []}).status_code == 503


def test_comparison_route_rejects_unknown_product(client):
    http, conn = client
    conn.fetch.side_effect = [[], [], []]
    result = http.post('/api/catalog/grocery/basket-comparison', json={'items': [{'card_id': 'missing', 'quantity': 1}]})
    assert result.status_code == 422


def test_editorial_import_is_escaped_and_transactional():
    payload = GroceryImport(markets=[dict(id='a', name="Mercado d'Água", logo_url='https://example.org/logo', website_url='https://example.org')])
    sql = import_sql(payload)
    assert sql.startswith('BEGIN;') and sql.endswith('COMMIT;')
    assert "Mercado d''Água" in sql
    assert 'ON CONFLICT (id) DO UPDATE' in sql


def test_duplicate_import_rejected():
    row = dict(id='a', name='A', logo_url='https://example.org/logo', website_url='https://example.org')
    with pytest.raises(ValueError):
        import_sql(GroceryImport(markets=[row, row]))


def test_cards_return_package_totals_and_market_research_date(client):
    http, conn = client
    from datetime import datetime, timezone
    card = dict(id='butter-200g', name='Manteiga', category='Laticínios',
        image_url='https://example.org/image.png', package_display='Pote 200g',
        package_net_value=200, package_unit='g', kcal_per_package=1400,
        carbs_g=0, protein_g=1, fat_g=160, price_avg='12.90', linked_ingredient_slug=None,
        clinical_tags=[], active=True, updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    quote = dict(card_id=card['id'], market_id='a', market_name='A',
        market_logo_url='https://example.org/logo.png', website_url='https://example.org',
        product_store_url=None, price='12.90', last_updated=datetime(2026, 10, 1, tzinfo=timezone.utc))
    conn.fetch.side_effect = [[card], [quote]]
    result = http.get('/api/catalog/grocery/cards')
    assert result.status_code == 200
    product = result.json()[0]
    assert product['package_unit'] == 'g'
    assert float(product['package_net_value']) == 200
    assert float(product['kcal_per_package']) == 1400
    assert product['updated_at'].startswith('2026-09-01')
    assert product['markets'][0]['last_updated'].startswith('2026-10-01')
    assert float(product['markets'][0]['price']) == 12.90


def test_editorial_import_requires_real_research_timestamp_with_timezone():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GroceryImport(prices=[dict(card_id='a', market_id='b', price='1.00', last_updated='2026-10-01T12:00:00')])
