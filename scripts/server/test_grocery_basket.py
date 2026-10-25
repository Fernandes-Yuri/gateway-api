from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from schemas.grocery_cards import BasketComparisonIn
from services.grocery_basket import cart_url, compare_basket, load_comparison


def market(id='a', **changes):
    return dict(id=id, name=id, logo_url='', website_url='https://shop.test',
                cart_type='VTEX', cart_url_template=None, **changes)


def price(market_id, card_id, price, **changes):
    return dict(market_id=market_id, card_id=card_id, price=price,
                in_stock=changes.get('in_stock', True), product_sku=card_id,
                product_store_url='https://shop.test/product')


def test_full_coverage_ranks_before_cheaper_partial_and_savings_use_complete_baskets():
    result = compare_basket([market('a'), market('b'), market('c')],
        [dict(id='rice', name='Arroz'), dict(id='beans', name='Feijão')],
        [price('a', 'rice', '2.10'), price('a', 'beans', '4.20'),
         price('b', 'rice', '1.00'), price('b', 'beans', '1.00', in_stock=False),
         price('c', 'rice', '3.00'), price('c', 'beans', '5.00')], {'rice': 2, 'beans': 1})
    rows = result['market_rankings']
    assert [r['market_id'] for r in rows] == ['a', 'c', 'b']
    assert rows[0]['total_brl'] == 8.4
    assert rows[0]['savings_vs_highest_brl'] == 2.6
    assert rows[0]['is_best_deal'] and result['best_market'] == rows[0]
    assert rows[2]['missing_items'] == ['beans'] and rows[2]['coverage'] == .5
    assert rows[2]['savings_vs_highest_brl'] is None


def test_all_missing_no_fabricated_winner_or_url():
    result = compare_basket([market()], [dict(id='a', name='A')], [], {'a': 1})
    assert result['best_market'] is None
    row = result['market_rankings'][0]
    assert row['missing_items_count'] == 1 and row['super_url'] is None
    assert row['total_brl'] == 0 and not row['is_best_deal']


@pytest.mark.parametrize('kind,template,expected', [
    ('VTEX', None, 'https://shop.test/checkout/cart/add?sku=123&qty=2&seller=1&sku=456&qty=1&seller=1'),
    ('VTEX', 'https://shop.test/add?{items}', 'https://shop.test/add?sku=123&qty=2&seller=1&sku=456&qty=1&seller=1'),
    ('SHOPIFY', None, 'https://shop.test/cart/123:2,456:1'),
    ('CUSTOM_PARAM', 'https://shop.test/add?{items}', 'https://shop.test/add?sku=123&qty=2&seller=1&sku=456&qty=1&seller=1'),
    ('CUSTOM_PARAM', None, None), ('ASSISTED', None, None),
])
def test_cart_urls(kind, template, expected):
    m = market()
    m.update(cart_type=kind, cart_url_template=template)
    assert cart_url(m, [dict(available=True, product_sku='123', quantity=2),
                        dict(available=True, product_sku='456', quantity=1)]) == expected


def test_missing_sku_disables_cart_and_skus_are_encoded():
    assert cart_url(market(), [dict(available=True, product_sku=None, quantity=1)]) is None
    m = market()
    m['cart_type'] = 'SHOPIFY'
    assert cart_url(m, [dict(available=True, product_sku='a/b&c', quantity=1)]).endswith('/cart/a%2Fb%26c:1')


def test_assisted_returns_direct_links_and_quantities():
    m = market()
    m['cart_type'] = 'ASSISTED'
    row = compare_basket([m], [dict(id='a', name='A')], [price('a', 'a', '1.25')], {'a': 2})['best_market']
    assert row['super_url'] is None
    assert row['assisted_links'] == [dict(card_id='a', name='A', quantity=2, url='https://shop.test/product')]


@pytest.mark.parametrize('items', [[], [dict(card_id='a', quantity=0)],
    [dict(card_id='a', quantity=1.5)], [dict(card_id='a', quantity=True)],
    [dict(card_id='a', quantity=1)] * 2])
def test_invalid_baskets_rejected(items):
    with pytest.raises(ValueError):
        BasketComparisonIn(items=items)


async def test_unknown_or_inactive_card_rejected_before_price_lookup():
    conn = AsyncMock()
    conn.fetch.return_value = []
    with pytest.raises(ValueError, match='inativos'):
        await load_comparison(conn, {'unknown': 1})
    assert conn.fetch.await_count == 1


def test_public_endpoint_and_validation(monkeypatch):
    from routers import catalog
    from unittest.mock import MagicMock
    pool, conn = MagicMock(), AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    mock = AsyncMock(return_value={'market_rankings': [], 'best_market': None, 'total_items': 1})
    monkeypatch.setattr(catalog, 'load_comparison', mock)
    catalog.configure_state(db_pool=pool)
    app = FastAPI()
    app.include_router(catalog.router)
    try:
        with TestClient(app) as client:
            path = '/api/catalog/grocery/basket-comparison'
            assert client.post(path, json={'items': []}).status_code == 422
            assert client.post(path, json={'items': [{'card_id': 'a', 'quantity': 2}]}).status_code == 200
            mock.assert_awaited_once_with(conn, {'a': 2})
    finally:
        catalog.configure_state()
