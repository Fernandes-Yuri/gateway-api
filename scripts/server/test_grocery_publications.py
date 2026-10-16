from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import hashlib
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import catalog
from schemas.grocery_editorial import PublishEdition, ChangePublication
from services.grocery_editorial import preview_edition, EditorialConflict
from services.grocery_publications import freeze_preview, publish, change_publication, public_feed, today
from test_grocery_editorial import card, market, quote, snapshot


def reviewed():
    data = snapshot([quote('rice', 'a', '10'), quote('eggs', 'a', '8')])
    data['edition'].update(title='Semana', starts_on=today(), ends_on=today()+timedelta(days=6), notes='Anotação privada')
    cards = [card('rice'), card('eggs')]
    markets = [market('a')]
    return data, cards, markets, preview_edition(data, cards, markets)


def payload(preview):
    return PublishEdition(request_id=uuid4(), expected_revision=1, expected_publication_id=None,
        review_digest=preview['review_digest'], pack_ids=['pack-a'])


def test_freeze_contains_only_selected_reviewed_packs_and_excludes_private_notes():
    _, _, _, preview = reviewed()
    frozen = freeze_preview(preview, ['pack-a'])
    assert not frozen['is_preview']
    assert 'notes' not in frozen['edition']
    assert 'revision' not in frozen['edition']
    assert frozen['packs'][0]['market']['id'] == 'a'
    with pytest.raises(ValueError): freeze_preview(preview, ['outside'])
    preview['packs'][0]['ready_for_review'] = False
    with pytest.raises(ValueError): freeze_preview(preview, ['pack-a'])


async def test_publish_requires_the_exact_review_and_writes_an_atomic_snapshot():
    draft, cards, markets, preview = reviewed()
    conn = AsyncMock(); conn.fetchrow.return_value = None; conn.fetch.return_value = markets
    conn.fetchval.side_effect = [False, 7]
    with patch('services.grocery_publications.locked_head', AsyncMock()), \
         patch('services.grocery_publications.edition_snapshot', AsyncMock(return_value=draft)), \
         patch('services.grocery_publications.load_cards', AsyncMock(return_value=cards)), \
         patch('services.grocery_publications.history', AsyncMock(return_value={'ok': True})):
        assert await publish(conn, 'week-a', payload(preview)) == {'ok': True}
    args = conn.execute.call_args_list[0].args
    assert 'INSERT INTO grocery_publications' in args[0]
    frozen = json.loads(args[7])
    assert frozen['packs'][0]['total_brl'] == '28'
    assert frozen['edition']['title'] == 'Semana'
    assert 'notes' not in frozen['edition']
    assert conn.execute.call_args_list[1].args[0].startswith('UPDATE grocery_publication_heads')
    assert all('grocery_edition_quotes' not in call.args[0] for call in conn.execute.call_args_list)


async def test_changed_catalog_since_review_cannot_publish():
    draft, cards, markets, preview = reviewed()
    cards[0]['name'] = 'Nome alterado depois da revisão'
    conn = AsyncMock(); conn.fetchrow.return_value = None; conn.fetch.return_value = markets; conn.fetchval.return_value = False
    with patch('services.grocery_publications.locked_head', AsyncMock()), \
         patch('services.grocery_publications.edition_snapshot', AsyncMock(return_value=draft)), \
         patch('services.grocery_publications.load_cards', AsyncMock(return_value=cards)), pytest.raises(EditorialConflict):
        await publish(conn, 'week-a', payload(preview))
    conn.execute.assert_not_awaited()


async def test_retry_is_idempotent_but_changed_request_with_same_id_is_rejected():
    _, _, _, preview = reviewed(); body = payload(preview)
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
    conn = AsyncMock(); conn.fetchrow.return_value = dict(id=str(body.request_id), edition_id='week-a', source_revision=1, request_fingerprint=fingerprint)
    with patch('services.grocery_publications.history', AsyncMock(return_value={})):
        await publish(conn, 'week-a', body)
    conn.execute.assert_not_awaited()
    body.pack_ids = ['another-pack']
    with pytest.raises(EditorialConflict): await publish(conn, 'week-a', body)


async def test_restoration_and_withdrawal_change_only_the_head_and_feed_revision():
    conn = AsyncMock(); conn.fetchrow.return_value = dict(id='old', ends_on=today()+timedelta(days=6))
    body = ChangePublication(action='restore', publication_id='old', expected_publication_id='new')
    with patch('services.grocery_publications.locked_head', AsyncMock()), patch('services.grocery_publications.history', AsyncMock(return_value={})):
        await change_publication(conn, 'week-a', body)
    assert conn.execute.call_args_list[0].args[1:] == ('week-a', 'old')
    assert all('UPDATE grocery_publications ' not in c.args[0] for c in conn.execute.call_args_list)
    conn.reset_mock(); body = ChangePublication(action='withdraw', publication_id='old', expected_publication_id='old')
    with patch('services.grocery_publications.locked_head', AsyncMock()), patch('services.grocery_publications.history', AsyncMock(return_value={})):
        await change_publication(conn, 'week-a', body)
    assert conn.execute.call_args_list[0].args[1:] == ('week-a', None)


async def test_expired_version_cannot_be_restored():
    conn = AsyncMock(); conn.fetchrow.return_value = dict(id='old', ends_on=today()-timedelta(days=1))
    with patch('services.grocery_publications.locked_head', AsyncMock()), pytest.raises(ValueError, match='venceu'):
        await change_publication(conn, 'week-a', ChangePublication(action='restore', publication_id='old', expected_publication_id=None))
    conn.execute.assert_not_awaited()


async def test_public_feed_reads_only_active_heads_in_the_current_validity_window():
    conn = AsyncMock(); conn.fetchval.return_value = 8; conn.fetch.return_value = []
    result = await public_feed(conn)
    assert result['revision'] == 8 and result['publications'] == []
    sql, date = conn.fetch.call_args.args
    assert 'grocery_publication_heads' in sql
    assert 'p.starts_on<=$1 AND p.ends_on>=$1' in sql
    assert date == today()


@pytest.fixture
def client(monkeypatch):
    conn = AsyncMock(); conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(); pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    catalog.configure_state(db_pool=pool)
    monkeypatch.setattr(catalog, '_service_token', lambda: 'publications-test-only')
    app = FastAPI(); app.include_router(catalog.router)
    yield TestClient(app), conn
    catalog.configure_state()


def test_public_feed_is_readable_without_admin_session_and_administration_is_private(client):
    http, conn = client; conn.fetchval.return_value = 1; conn.fetch.return_value = []
    response = http.get('/api/catalog/grocery/publications')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['publications'] == []
    assert http.get('/api/catalog/grocery/publications/revision').status_code == 200
    assert http.get('/api/admin/grocery/editions/week-a/publications').status_code == 401
    assert http.post('/api/admin/grocery/editions/week-a/publications', json={}).status_code == 401
    assert http.put('/api/admin/grocery/editions/week-a/publications', json={}).status_code == 401


async def test_feed_filters_periods_and_normalizes_legacy_without_mutating_snapshots():
    _, _, _, preview = reviewed()
    frozen = freeze_preview(preview, ['pack-a'])
    monthly = json.loads(json.dumps(frozen, default=str))['packs'][0]
    monthly['pack']['id'] = 'monthly'
    monthly['pack']['planning_days'] = 30
    frozen['packs'].append(monthly)
    original = json.dumps(frozen, default=str)
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    conn.fetch.return_value = [dict(id='publication', version=1, published_at='2026-10-16', snapshot=original)]
    all_periods = await public_feed(conn)
    assert all_periods['planning_period_options'] == [7, 14, 30]
    assert [p['pack']['planning_days'] for p in all_periods['publications'][0]['packs']] == [7, 30]
    assert [p['pack']['id'] for p in (await public_feed(conn, 30))['publications'][0]['packs']] == ['monthly']
    assert (await public_feed(conn, 14))['publications'] == []
    assert conn.fetch.return_value[0]['snapshot'] == original
    conn.execute.assert_not_awaited()


def test_public_feed_accepts_supported_query_period_and_rejects_other_values(client):
    http, conn = client
    conn.fetchval.return_value = 1
    conn.fetch.return_value = []
    for days in [7, 14, 30]:
        assert http.get(f'/api/catalog/grocery/publications?planning_days={days}').status_code == 200
    for days in ['15', '0', 'abc']:
        assert http.get(f'/api/catalog/grocery/publications?planning_days={days}').status_code == 422
