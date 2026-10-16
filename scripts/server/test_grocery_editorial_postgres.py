"""Exercise migration and transactions only on the isolated local CI database."""
import os
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
import asyncpg
import pytest
from schemas.grocery_editorial import EditionCreate, EditorialMutation, PublishEdition, ChangePublication
from unittest.mock import patch
from services.grocery_publications import publish, public_feed, change_publication
from services.grocery_editorial import preview_edition
from services.grocery_cards import load_cards
from services.grocery_retail_admin import save_record
from services.grocery_editorial import create_edition, mutate_edition, edition_snapshot, EditorialConflict

DSN = os.getenv('TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='PostgreSQL efêmero disponível no CI')
ROOT = Path(__file__).resolve().parents[2]


async def test_weekly_workspace_clone_isolation_and_transactional_rollback():
    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'editorial_' + uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute('CREATE TABLE grocery_ingredients(slug TEXT PRIMARY KEY)')
        await conn.execute((ROOT / 'migrations/058_grocery_retail_cards_and_markets.sql').read_text())
        await conn.execute((ROOT / 'migrations/061_grocery_dietary_fiber.sql').read_text())
        await conn.execute((ROOT / 'migrations/059_grocery_editorial_workspace.sql').read_text())
        await conn.execute("INSERT INTO grocery_markets(id,name,logo_url,website_url) VALUES('a','Mercado A','https://example.org/logo','https://example.org')")
        await conn.execute("""INSERT INTO grocery_retail_cards(id,name,category,image_url,package_display,package_net_value,package_unit,kcal_per_package,price_avg)
            VALUES('rice','Arroz','Mercearia','https://example.org/rice','Pacote 1kg',1000,'g',3500,10)""")
        async with conn.transaction():
            original = await create_edition(conn, EditionCreate(title='Semana A', starts_on='2026-10-19', ends_on='2026-10-25', region='Rio'))
        edition_id = original['edition']['id']
        async with conn.transaction():
            original = await mutate_edition(conn, edition_id, EditorialMutation(expected_revision=1, operation='quote', payload=dict(
                card_id='rice', market_id='a', price='9.90', researched_at='2026-01-01T12:00:00Z', confirmed=True,
                product_store_url='https://example.org/rice', minimum_quantity=1, conditions='Loja do bairro')))
        async with conn.transaction():
            original = await mutate_edition(conn, edition_id, EditorialMutation(expected_revision=2, operation='pack', payload=dict(
                market_id='a', name='Pack A', items=[dict(card_id='rice', quantity=2)])))
        async with conn.transaction():
            clone = await create_edition(conn, EditionCreate(title='Semana B', starts_on='2026-10-26', ends_on='2026-11-01',
                region='Rio', source_edition_id=edition_id))
        assert clone['edition']['id'] != edition_id
        assert clone['quotes'][0]['confirmed'] is False
        assert clone['quotes'][0]['researched_at'] == original['quotes'][0]['researched_at']
        assert clone['packs'][0]['id'] != original['packs'][0]['id']
        assert clone['packs'][0]['items'] == original['packs'][0]['items']
        async with conn.transaction():
            await mutate_edition(conn, clone['edition']['id'], EditorialMutation(expected_revision=1, operation='quote', payload=dict(
                card_id='rice', market_id='a', price='8.90', researched_at='2026-01-01T12:00:00Z')))
        untouched = await edition_snapshot(conn, edition_id)
        assert str(untouched['quotes'][0]['price']) == '9.90'
        assert untouched['edition']['revision'] == 3
        with pytest.raises(EditorialConflict):
            async with conn.transaction():
                await mutate_edition(conn, edition_id, EditorialMutation(expected_revision=1, operation='remove_quote', payload=dict(card_id='rice', market_id='a')))
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await mutate_edition(conn, edition_id, EditorialMutation(expected_revision=3, operation='pack', payload=dict(
                    id=original['packs'][0]['id'], market_id='a', name='Inválido', items=[dict(card_id='missing', quantity=1)])))
        restored = await edition_snapshot(conn, edition_id)
        assert restored['packs'][0]['items'] == original['packs'][0]['items']
        assert restored['packs'][0]['name'] == 'Pack A'
        assert restored['edition']['revision'] == 3
        assert await conn.fetchval('SELECT COUNT(*) FROM grocery_market_prices') == 0
        await conn.execute((ROOT / 'migrations/060_grocery_editorial_publications.sql').read_text())
        draft = await edition_snapshot(conn, edition_id)
        preview = preview_edition(draft, await load_cards(conn, include_inactive=True),
            await conn.fetch('SELECT * FROM grocery_markets ORDER BY name,id'))
        with patch('services.grocery_publications.today', return_value=draft['edition']['starts_on']):
            body = PublishEdition(request_id=uuid4(), expected_revision=3, expected_publication_id=None,
                review_digest=preview['review_digest'], pack_ids=[draft['packs'][0]['id']])
            async with conn.transaction(isolation='repeatable_read'):
                released = await publish(conn, edition_id, body)
            publication_id = released['current_publication_id']
            source_card = dict((await load_cards(conn))[0])
            source_card.pop('markets', None)
            source_card['active'] = False
            await save_record(conn, 'card', source_card)
            assert await load_cards(conn) == []
            # An editorial withdrawal removes the live card without corrupting a published snapshot.
            assert (await public_feed(conn))['publications'][0]['packs'][0]['items'][0]['product']['id'] == 'rice'
            source_card.update(id='new-product', name='Novo produto', active=True, image_url='https://example.org/new.png')
            await save_record(conn, 'card', source_card)
            assert (await load_cards(conn))[0]['id'] == 'new-product'
            source_card.update(id='rice', name='Arroz editado', image_url='https://example.org/edited.png')
            await save_record(conn, 'card', source_card)
            current = next(c for c in await load_cards(conn) if c['id'] == 'rice')
            assert current['name'] == 'Arroz editado'
            assert current['image_url'] == 'https://example.org/edited.png'
            frozen_card = (await public_feed(conn))['publications'][0]['packs'][0]['items'][0]['product']
            assert frozen_card['name'] == 'Arroz'
            assert frozen_card['image_url'] == 'https://example.org/rice'
            assert (await public_feed(conn))['publications'][0]['packs'][0]['total_brl'] == '19.80'
            async with conn.transaction():
                await mutate_edition(conn, edition_id, EditorialMutation(expected_revision=3, operation='quote', payload=dict(
                    card_id='rice', market_id='a', price='1.00', researched_at='2026-01-01T12:00:00Z',
                    confirmed=True, product_store_url='https://example.org/rice')))
            assert (await public_feed(conn))['publications'][0]['packs'][0]['total_brl'] == '19.80'
            with pytest.raises(asyncpg.RaiseError):
                await conn.execute("UPDATE grocery_publications SET snapshot='{}' WHERE id=$1", publication_id)
            async with conn.transaction():
                await change_publication(conn, edition_id, ChangePublication(action='withdraw', publication_id=publication_id,
                    expected_publication_id=publication_id))
            assert (await public_feed(conn))['publications'] == []
            async with conn.transaction():
                await change_publication(conn, edition_id, ChangePublication(action='restore', publication_id=publication_id,
                    expected_publication_id=None))
            assert (await public_feed(conn))['publications'][0]['packs'][0]['total_brl'] == '19.80'
        await conn.execute((ROOT / 'migrations/060_grocery_editorial_publications_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/059_grocery_editorial_workspace_rollback.sql').read_text())
        assert await conn.fetchval('SELECT COUNT(*) FROM grocery_retail_cards') == 2
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


async def test_food_rename_is_atomic_in_both_directions_and_keeps_prices():
    from services.ingredient_admin import update_ingredient

    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'names_' + uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""CREATE TABLE grocery_ingredients(
            slug TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}', active BOOLEAN NOT NULL DEFAULT TRUE);
            CREATE TABLE nutrition_catalog_settings(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
            INSERT INTO nutrition_catalog_settings VALUES(1,1);
            CREATE TABLE grocery_price_cache(food_name TEXT PRIMARY KEY, ingredient_slug TEXT,
                price_unit TEXT, price_avg NUMERIC, updated_at TIMESTAMPTZ DEFAULT now());
            INSERT INTO grocery_ingredients(slug,name,unit) VALUES('avocado','Abacate Manteiga','kg');
            INSERT INTO grocery_price_cache(food_name,price_unit,price_avg)
                VALUES('Abacate Manteiga','kg',6.0);""")
        await conn.execute((ROOT / 'migrations/058_grocery_retail_cards_and_markets.sql').read_text())
        await conn.execute((ROOT / 'migrations/061_grocery_dietary_fiber.sql').read_text())
        await conn.execute("""INSERT INTO grocery_retail_cards
            (id,name,category,image_url,package_display,package_net_value,package_unit,kcal_per_package,price_avg,linked_ingredient_slug)
            VALUES('avocado-430','Abacate','Frutas','https://example.org/a.jpg','430 g',430,'g',412,2.58,'avocado'),
                  ('avocado-500','Abacate Manteiga','Frutas','https://example.org/b.jpg','500 g',500,'g',479,3.0,'avocado');
            INSERT INTO grocery_markets(id,name,logo_url,website_url)
                VALUES('market','Mercado','https://example.org/logo.png','https://example.org');
            INSERT INTO grocery_market_prices(card_id,market_id,price,product_store_url)
                VALUES('avocado-430','market',2.58,'https://example.org/a');""")
        price_before = dict(await conn.fetchrow('SELECT * FROM grocery_market_prices'))
        cache_before = dict(await conn.fetchrow('SELECT * FROM grocery_price_cache'))
        product = dict(id='avocado-430', name='Abacate Selecionado', category='Frutas',
                       image_url='https://example.org/a.jpg', package_display='430 g',
                       package_net_value=430, package_unit='g', kcal_per_package=412,
                       price_avg=2.58, fiber_g=12.5, linked_ingredient_slug='avocado')
        async with conn.transaction():
            await save_record(conn, 'card', product)
        assert await conn.fetchval("SELECT name FROM grocery_ingredients WHERE slug='avocado'") == 'Abacate Selecionado'
        assert {r['name'] for r in await conn.fetch('SELECT name FROM grocery_retail_cards')} == {'Abacate Selecionado'}
        assert dict(await conn.fetchrow('SELECT * FROM grocery_market_prices')) == price_before
        cache_after = dict(await conn.fetchrow('SELECT * FROM grocery_price_cache'))
        assert cache_after['ingredient_slug'] == 'avocado'
        assert cache_after['price_avg'] == cache_before['price_avg']
        assert cache_after['updated_at'] == cache_before['updated_at']
        async with conn.transaction():
            await update_ingredient(conn, 'avocado', {'name': 'Abacate Especial'})
        assert {r['name'] for r in await conn.fetch('SELECT name FROM grocery_retail_cards')} == {'Abacate Especial'}
        assert await conn.fetchval("SELECT kcal_per_package FROM grocery_retail_cards WHERE id='avocado-430'") == 412
        assert await conn.fetchval("SELECT package_net_value FROM grocery_retail_cards WHERE id='avocado-430'") == 430
        assert await conn.fetchval("SELECT fiber_g FROM grocery_retail_cards WHERE id='avocado-430'") == 12.5
        revision = await conn.fetchval('SELECT revision FROM nutrition_catalog_settings WHERE id=1')
        with pytest.raises(RuntimeError, match='falha deliberada'):
            async with conn.transaction():
                await save_record(conn, 'card', {**product, 'name': 'Abacate Temporário'})
                raise RuntimeError('falha deliberada')
        assert await conn.fetchval("SELECT name FROM grocery_ingredients WHERE slug='avocado'") == 'Abacate Especial'
        assert {r['name'] for r in await conn.fetch('SELECT name FROM grocery_retail_cards')} == {'Abacate Especial'}
        assert await conn.fetchval('SELECT revision FROM nutrition_catalog_settings WHERE id=1') == revision
        assert dict(await conn.fetchrow('SELECT * FROM grocery_market_prices')) == price_before
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
