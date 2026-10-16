"""Weekly drafts, single-market composition, and publication preview invariants."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from routers import catalog
from schemas.grocery_editorial import EditionCreate, PackDraft, EditorialQuote, EditorialMutation
from services.grocery_editorial import preview_edition, mutate_edition, create_edition, EditorialConflict


def card(id, category='Mercearia', active=True):
    return dict(id=id, name=id, category=category, package_display='Pacote 200g', active=active, kcal_per_package=320)


def market(id, active=True):
    return dict(id=id, name=id, active=active, website_url='https://example.org')


def quote(card_id, market_id, price, **kwargs):
    return dict(card_id=card_id, market_id=market_id, price=Decimal(price), confirmed=True,
                product_store_url='https://example.org/item', minimum_quantity=1, membership_required=False,
                conditions='', researched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), **kwargs)


def snapshot(quotes=None, items=None):
    return dict(edition=dict(id='week-a', revision=1, region='Rio'), quotes=quotes or [], packs=[dict(
        id='pack-a', edition_id='week-a', name='Cesta', market_id='a', pack_type='complete', category=None,
        items=items if items is not None else [dict(card_id='rice', quantity=2), dict(card_id='eggs', quantity=1)])])


def test_preview_compares_complete_single_market_baskets_without_mixing_prices():
    data = snapshot([quote('rice', 'a', '10'), quote('eggs', 'a', '8'),
                     quote('rice', 'b', '8'), quote('eggs', 'b', '15'), quote('rice', 'partial', '1')])
    result = preview_edition(data, [card('rice'), card('eggs')], [market('a'), market('b'), market('partial')])
    pack = result['packs'][0]
    assert result['is_preview'] and result['ready_for_review']
    assert pack['total_brl'] == Decimal('28')  # Never 2*8 + 8 from different stores.
    assert pack['total_planned_kcal'] == 960
    assert pack['comparison_complete']
    assert pack['savings_vs_highest_brl'] == Decimal('3')
    assert pack['comparison'][0]['market']['id'] == 'a'
    assert not pack['comparison'][-1]['is_best_deal']
    assert pack['comparison'][-1]['missing_items_count'] == 1
    assert all(line['quote']['market_id'] == 'a' for line in pack['items'])


def test_pending_research_does_not_count_as_a_confirmed_price():
    pending = quote('rice', 'a', '10'); pending['confirmed'] = False
    result = preview_edition(snapshot([pending, quote('eggs', 'a', '8')]), [card('rice'), card('eggs')], [market('a')])
    pack = result['packs'][0]
    assert not pack['ready_for_review']
    assert pack['items'][0]['subtotal_brl'] is None
    assert pack['total_brl'] == 8
    assert pack['savings_vs_highest_brl'] is None


def test_minimum_order_is_enforced_and_membership_conditions_are_visible():
    rice = quote('rice', 'a', '10'); rice.update(minimum_quantity=3, membership_required=True, conditions='Clube')
    data = snapshot([rice, quote('eggs', 'a', '8')])
    pack = preview_edition(data, [card('rice'), card('eggs')], [market('a')])['packs'][0]
    assert not pack['ready_for_review']
    data['packs'][0]['items'][0]['quantity'] = 3
    pack = preview_edition(data, [card('rice'), card('eggs')], [market('a')])['packs'][0]
    assert pack['ready_for_review']
    assert pack['comparison'][0]['conditions'][0]['membership_required'] is True
    assert pack['comparison'][0]['conditions'][0]['conditions'] == 'Clube'


def test_category_inactive_market_and_empty_pack_are_review_blockers():
    data = snapshot()
    data['packs'][0].update(pack_type='category', category='Hortifruti')
    result = preview_edition(data, [card('rice'), card('eggs')], [market('a', active=False)])
    assert not result['ready_for_review']
    assert any('categoria' in issue for issue in result['packs'][0]['issues'])
    assert any('inativo' in issue for issue in result['packs'][0]['issues'])
    assert not preview_edition(snapshot(items=[]), [], [market('a')])['ready_for_review']
    assert not preview_edition(dict(edition={}, quotes=[], packs=[]), [], [])['ready_for_review']


def test_inactive_product_and_missing_source_block_readiness():
    source = quote('rice', 'a', '10'); source['product_store_url'] = None
    result = preview_edition(snapshot([source, quote('eggs', 'a', '8')]), [card('rice', active=False), card('eggs')], [market('a')])
    assert not result['ready_for_review']
    assert any('inativo' in i for i in result['packs'][0]['issues'])


def test_input_validation_rejects_repeated_products_fractional_quantities_and_invalid_dates():
    for items in [[dict(card_id='a', quantity=1), dict(card_id='a', quantity=2)], [dict(card_id='a', quantity=1.5)], [dict(card_id='a', quantity=True)]]:
        with pytest.raises(ValidationError):
            PackDraft(name='Cesta', market_id='a', items=items)
    with pytest.raises(ValidationError):
        EditionCreate(title=' ', starts_on='2026-10-19', ends_on='2026-10-25', region='Rio')
    with pytest.raises(ValidationError):
        EditionCreate(title='Semana', starts_on='2026-10-25', ends_on='2026-10-19', region='Rio')
    with pytest.raises(ValidationError):
        PackDraft(name='Categoria', market_id='a', pack_type='category')
    with pytest.raises(ValidationError):
        EditorialQuote(card_id='a', market_id='a', price=10, confirmed=True, researched_at='2026-01-01T12:00:00Z')
    with pytest.raises(ValidationError):
        EditorialQuote(card_id='a', market_id='a', price=10, researched_at='2026-01-01T12:00:00')


async def test_concurrent_revision_conflict_stops_before_any_write():
    conn = AsyncMock(); conn.fetchval.return_value = 5
    body = EditorialMutation(expected_revision=4, operation='remove_quote', payload=dict(card_id='a', market_id='a'))
    with pytest.raises(EditorialConflict):
        await mutate_edition(conn, 'week-a', body)
    assert 'FOR UPDATE' in conn.fetchval.call_args.args[0]
    conn.execute.assert_not_awaited()


async def test_pack_cannot_be_edited_or_deleted_through_another_edition():
    body = EditorialMutation(expected_revision=1, operation='pack', payload=dict(id='pack-other', name='Pack', market_id='a'))
    conn = AsyncMock(); conn.fetchval.side_effect = [1, 'week-other']
    with pytest.raises(LookupError):
        await mutate_edition(conn, 'week-a', body)
    conn.execute.assert_not_awaited()
    conn = AsyncMock(); conn.fetchval.side_effect = [1, None]
    body = EditorialMutation(expected_revision=1, operation='remove_pack', payload=dict(id='pack-other'))
    with pytest.raises(LookupError):
        await mutate_edition(conn, 'week-a', body)


async def test_weekly_quote_writes_only_private_table_and_advances_revision():
    conn = AsyncMock(); conn.fetchval.return_value = 1
    payload = dict(card_id='a', market_id='b', price='12.90', researched_at='2026-01-01T12:00:00Z')
    body = EditorialMutation(expected_revision=1, operation='quote', payload=payload)
    with patch('services.grocery_editorial.edition_snapshot', AsyncMock(return_value={})):
        await mutate_edition(conn, 'week-a', body)
    calls = conn.execute.call_args_list
    assert 'INSERT INTO grocery_edition_quotes' in calls[0].args[0]
    assert 'grocery_market_prices' not in calls[0].args[0]
    assert calls[0].args[1:4] == ('week-a', 'a', 'b')
    assert 'revision=revision+1' in calls[1].args[0]


async def test_duplication_resets_confirmation_without_refreshing_research_timestamp():
    conn = AsyncMock(); conn.fetchval.return_value = 'old'
    body = EditionCreate(title='Nova semana', starts_on='2026-10-19', ends_on='2026-10-25', region='Rio', source_edition_id='old')
    with patch('services.grocery_editorial.edition_snapshot', AsyncMock(side_effect=[dict(packs=[]), dict(edition=dict(id='new'))])):
        await create_edition(conn, body)
    copy_sql = conn.execute.call_args_list[1].args[0]
    assert 'researched_at,FALSE' in copy_sql
    assert 'now()' not in copy_sql
    assert conn.execute.call_args_list[1].args[2] == 'old'


@pytest.fixture
def client(monkeypatch):
    conn = AsyncMock(); conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(); pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    catalog.configure_state(db_pool=pool)
    monkeypatch.setattr(catalog, '_service_token', lambda: 'editorial-test-only')
    app = FastAPI(); app.include_router(catalog.router)
    yield TestClient(app), conn
    catalog.configure_state()


AUTH = dict(Authorization='Bearer editorial-test-only')


def test_editorial_routes_are_private_and_report_conflicts(client):
    http, conn = client
    for path in ['/api/admin/grocery/editions', '/api/admin/grocery/editions/a', '/api/admin/grocery/editions/a/preview']:
        assert http.get(path).status_code == 401
    assert http.post('/api/admin/grocery/editions', json={}).status_code == 401
    conn.fetchval.return_value = 2
    result = http.put('/api/admin/grocery/editions/a', headers=AUTH,
        json=dict(expected_revision=1, operation='remove_quote', payload=dict(card_id='a', market_id='b')))
    assert result.status_code == 409
    conn.execute.assert_not_awaited()


def test_unknown_edition_is_404_and_database_unavailable_is_503(client):
    http, conn = client
    conn.fetchrow.return_value = None
    assert http.get('/api/admin/grocery/editions/missing', headers=AUTH).status_code == 404
    catalog.configure_state()
    assert http.get('/api/admin/grocery/editions', headers=AUTH).status_code == 503


@pytest.mark.parametrize('days', [7, 14, 30])
def test_pack_period_is_independent_of_price_validity_and_keeps_exact_quantities(days):
    pack = PackDraft(name='Compra', market_id='a', planning_days=days,
                     items=[dict(card_id='rice', quantity=2)])
    data = snapshot([quote('rice', 'a', '10')], items=[dict(card_id='rice', quantity=2)])
    data['packs'][0]['planning_days'] = pack.planning_days
    result = preview_edition(data, [card('rice')], [market('a')])
    assert result['packs'][0]['pack']['planning_days'] == days
    assert result['packs'][0]['total_brl'] == 20
    assert result['packs'][0]['total_planned_kcal'] == 640


@pytest.mark.parametrize('days', [0, 1, 15, 31, True, 7.0, '14', None])
def test_pack_rejects_unsupported_or_coerced_period(days):
    with pytest.raises(ValidationError):
        PackDraft(name='Compra', market_id='a', planning_days=days)
