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
