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
    assert http.post('/api/catalog/grocery/basket-comparison', json={'items': [{'card_id': 'a', 'quantity': 1}]}).status_code == 503


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
    assert product['image_credit'] is None
    assert product['image_credit_url'] is None


def test_cards_expose_image_credit_and_url_when_available(client):
    http, conn = client
    from datetime import datetime, timezone
    card = dict(id='arroz-1kg', name='Arroz', category='Cereais',
        image_url='https://example.org/arroz.png', package_display='1 kg',
        package_net_value=1000, package_unit='g', kcal_per_package=3600,
        carbs_g=800, protein_g=70, fat_g=5, price_avg='7.50', linked_ingredient_slug=None,
        clinical_tags=[], active=True, updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        image_credit='Fotógrafo Fulano / Unsplash', image_credit_url='https://unsplash.com/photos/123')
    conn.fetch.side_effect = [[card], []]
    result = http.get('/api/catalog/grocery/cards')
    assert result.status_code == 200
    item = result.json()[0]
    assert item['image_credit'] == 'Fotógrafo Fulano / Unsplash'
    assert item['image_credit_url'] == 'https://unsplash.com/photos/123'


def test_editorial_import_requires_real_research_timestamp_with_timezone():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GroceryImport(prices=[dict(card_id='a', market_id='b', price='1.00', last_updated='2026-10-01T12:00:00')])


def test_basic_food_without_photo_is_valid():
    from schemas.grocery_cards import GroceryRetailCardIn
    card = GroceryRetailCardIn(id='arroz', name='Arroz', category='Cereais',
        package_display='1 kg', package_net_value=1000, package_unit='g',
        kcal_per_package=3600, price_avg='7.50')
    assert card.image_url == ''


def test_supplied_image_still_requires_valid_url():
    from schemas.grocery_cards import GroceryRetailCardIn
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GroceryRetailCardIn(id='arroz', name='Arroz', category='Cereais', image_url='invalid',
            package_display='1 kg', package_net_value=1000, package_unit='g',
            kcal_per_package=3600, price_avg='7.50')


@pytest.mark.asyncio
async def test_latest_observation_is_reference_even_when_more_expensive():
    from services.grocery_cards import load_cards
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [dict(id='arroz', price_avg='7.50')],
        [dict(card_id='arroz', market_id='new', price='9.00'),
         dict(card_id='arroz', market_id='old', price='6.00')],
    ]
    cards = await load_cards(conn)
    assert cards[0]['price_avg'] == '9.00'
    assert cards[0]['markets'][0]['market_id'] == 'new'
    assert 'ORDER BY p.last_updated DESC,m.id' in conn.fetch.call_args_list[1].args[0]


def test_pack_endpoints_are_retired(client):
    http, _ = client
    assert http.get('/api/catalog/grocery/publications').status_code == 404
    assert http.get('/api/admin/grocery/editions').status_code == 404


def test_market_source_only_requires_identification():
    from schemas.grocery_cards import MarketIn
    market = MarketIn(id='bairro', name='Mercado do Bairro')
    assert market.logo_url == ''
    assert market.website_url == ''
