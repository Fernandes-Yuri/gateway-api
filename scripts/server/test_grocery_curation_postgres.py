"""Exercise reversible curation against disposable PostgreSQL only."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse
import uuid

import asyncpg
import pytest

DSN = os.getenv('TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='PostgreSQL efêmero disponível no CI')
ROOT = Path(__file__).resolve().parents[2]


async def test_curation_preserves_basics_photos_legacy_links_and_history():
    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'curation_' + uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""
            CREATE TABLE grocery_ingredients(slug TEXT PRIMARY KEY,name TEXT,details JSONB,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE food_catalog(remote_id TEXT PRIMARY KEY,name TEXT,details JSONB,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE grocery_retail_cards(id TEXT PRIMARY KEY,linked_ingredient_slug TEXT,image_url TEXT,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE grocery_market_prices(card_id TEXT,market_id TEXT,price NUMERIC,product_store_url TEXT,last_updated TIMESTAMPTZ,PRIMARY KEY(card_id,market_id));
            CREATE TABLE grocery_edition_quotes(edition_id TEXT,card_id TEXT,market_id TEXT,price NUMERIC,product_store_url TEXT,researched_at TIMESTAMPTZ,confirmed BOOLEAN,membership_required BOOLEAN,minimum_quantity INTEGER,conditions TEXT);
            CREATE TABLE nutrition_catalog_settings(id INTEGER,revision INTEGER);
            INSERT INTO nutrition_catalog_settings VALUES(1,1);
        """)
        for slug, name, photo in [('arroz', 'Arroz', ''), ('taioba', 'Taioba', ''),
                                  ('kefir-de-leite-artesanal', 'Kefir de Leite Artesanal', 'https://example.org/kefir.jpg'),
                                  ('amaranto-em-flocos', 'Amaranto em Flocos', '')]:
            await conn.execute('INSERT INTO grocery_ingredients(slug,name,details) VALUES($1,$2,$3)',
                               slug, name, json.dumps({'image_url': photo}))
            food_name = 'Farinha de Aveia Fina' if slug == 'amaranto-em-flocos' else name
            await conn.execute('INSERT INTO food_catalog(remote_id,name,details) VALUES($1,$2,$3)',
                               slug, food_name, json.dumps({'shoppingComponents': [slug], 'image_url': photo}))
            await conn.execute('INSERT INTO grocery_retail_cards VALUES($1,$1,$2,TRUE)', slug, photo)
        await conn.execute("INSERT INTO food_catalog VALUES('prato','Prato composto','{\"shoppingComponents\":[\"arroz\",\"feijao\"]}',TRUE)")
        await conn.execute("INSERT INTO grocery_edition_quotes VALUES('week','arroz','fonte',9.00,NULL,now(),TRUE,FALSE,1,'')")
        await conn.execute((ROOT / 'migrations/065_grocery_reference_and_curation.sql').read_text())
        ingredients = {row['slug']: row['active'] for row in await conn.fetch('SELECT slug,active FROM grocery_ingredients')}
        assert ingredients == {'arroz': True, 'taioba': False, 'kefir-de-leite-artesanal': True, 'amaranto-em-flocos': True}
        foods = {row['remote_id']: row['active'] for row in await conn.fetch('SELECT remote_id,active FROM food_catalog')}
        assert foods['arroz'] and foods['amaranto-em-flocos']
        assert not foods['taioba'] and not foods['prato']
        assert await conn.fetchval('SELECT COUNT(*) FROM food_catalog') == 5
        assert await conn.fetchval("SELECT price FROM grocery_market_prices WHERE card_id='arroz'") == 9
        assert await conn.fetchval('SELECT revision FROM nutrition_catalog_settings') == 2
        await conn.execute((ROOT / 'migrations/065_grocery_reference_and_curation_rollback.sql').read_text())
        assert await conn.fetchval('SELECT bool_and(active) FROM grocery_ingredients')
        assert await conn.fetchval('SELECT bool_and(active) FROM food_catalog')
        assert await conn.fetchval('SELECT bool_and(active) FROM grocery_retail_cards')
        assert await conn.fetchval('SELECT revision FROM nutrition_catalog_settings') == 3
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()

async def test_purge_non_canonical_072_inactivates_items_and_reverses():
    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'purge_072_' + uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""
            CREATE TABLE grocery_ingredients(slug TEXT PRIMARY KEY,name TEXT,unit TEXT,details JSONB,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE food_catalog(remote_id TEXT PRIMARY KEY,name TEXT,details JSONB,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE grocery_retail_cards(id TEXT PRIMARY KEY,linked_ingredient_slug TEXT,image_url TEXT,active BOOLEAN DEFAULT TRUE);
            CREATE TABLE nutrition_catalog_settings(id INTEGER PRIMARY KEY,revision INTEGER);
            CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ DEFAULT now());
            INSERT INTO nutrition_catalog_settings VALUES(1,10);
        """)
        for slug, name in [
            ('feijao-carioca', 'Feijão Carioca'),
            ('bertalha', 'Bertalha'),
            ('taioba', 'Taioba'),
            ('arroz-branco-tipo-1', 'Arroz Branco Tipo 1'),
            ('melissa-fresca', 'Melissa Fresca'),
            ('maca-gala-com-casca', 'Maçã Gala')
        ]:
            await conn.execute(
                'INSERT INTO grocery_ingredients(slug,name,unit,details) VALUES($1,$2,$3,$4)',
                slug, name, 'kg', json.dumps({'food_ids': [f'food_{slug}']})
            )
            await conn.execute(
                'INSERT INTO food_catalog(remote_id,name,details) VALUES($1,$2,$3)',
                f'food_{slug}', name, json.dumps({'shoppingComponents': [slug]})
            )
            await conn.execute('INSERT INTO grocery_retail_cards VALUES($1,$2,$3,TRUE)', f'card_{slug}', slug, '')

        await conn.execute("INSERT INTO food_catalog VALUES('food_217','Chá de Cidreira','{\"shoppingComponents\":[\"melissa-fresca\",\"maca-gala-com-casca\"]}',TRUE)")

        await conn.execute((ROOT / 'migrations/072_purge_non_canonical_grocery_items.sql').read_text())

        ing_active = {r['slug']: r['active'] for r in await conn.fetch('SELECT slug,active FROM grocery_ingredients')}
        assert ing_active['feijao-carioca'] is True
        assert ing_active['arroz-branco-tipo-1'] is True
        assert ing_active['maca-gala-com-casca'] is True
        assert ing_active['bertalha'] is False
        assert ing_active['taioba'] is False
        assert ing_active['melissa-fresca'] is False

        food_active = {r['remote_id']: r['active'] for r in await conn.fetch('SELECT remote_id,active FROM food_catalog')}
        assert food_active['food_feijao-carioca'] is True
        assert food_active['food_arroz-branco-tipo-1'] is True
        assert food_active['food_bertalha'] is False
        assert food_active['food_taioba'] is False
        assert food_active['food_217'] is False

        card_active = {r['id']: r['active'] for r in await conn.fetch('SELECT id,active FROM grocery_retail_cards')}
        assert card_active['card_feijao-carioca'] is True
        assert card_active['card_bertalha'] is False

        assert await conn.fetchval('SELECT revision FROM nutrition_catalog_settings WHERE id=1') == 11
        assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='072_purge_non_canonical_grocery_items')") is True

        await conn.execute((ROOT / 'migrations/072_purge_non_canonical_grocery_items_rollback.sql').read_text())
        assert await conn.fetchval('SELECT bool_and(active) FROM grocery_ingredients') is True
        assert await conn.fetchval('SELECT bool_and(active) FROM food_catalog') is True
        assert await conn.fetchval('SELECT bool_and(active) FROM grocery_retail_cards') is True
        assert await conn.fetchval('SELECT revision FROM nutrition_catalog_settings WHERE id=1') == 12
        assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='072_purge_non_canonical_grocery_items')") is False
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
