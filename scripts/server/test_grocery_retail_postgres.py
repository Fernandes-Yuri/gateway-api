"""Retail migration and editorial imports against the existing ephemeral CI database."""
import os
import uuid
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlparse

import asyncpg
import pytest

from schemas.grocery_cards import GroceryImport
from seed_grocery_cards import import_sql
from services.grocery_cards import load_cards

DSN = os.getenv('TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='PostgreSQL efêmero disponível no CI')
ROOT = Path(__file__).resolve().parents[2]


async def test_migration_import_dimension_validation_and_rollback():
    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'retail_' + uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("CREATE TABLE grocery_ingredients(slug TEXT PRIMARY KEY, unit TEXT NOT NULL)")
        await conn.execute("INSERT INTO grocery_ingredients VALUES ('butter','kg')")
        await conn.execute((ROOT / 'migrations/058_grocery_retail_cards_and_markets.sql').read_text())
        await conn.execute((ROOT / 'migrations/061_grocery_dietary_fiber.sql').read_text())
        await conn.execute((ROOT / 'migrations/063_grocery_retail_nutrition_facts.sql').read_text())
        await conn.execute((ROOT / 'migrations/064_grocery_retail_cards_updated_at.sql').read_text())
        await conn.execute((ROOT / 'migrations/070_grocery_card_image_credits.sql').read_text())
        await conn.execute((ROOT / 'migrations/073_grocery_super_urls.sql').read_text())
        await conn.execute((ROOT / 'migrations/074_grocery_unified_curation.sql').read_text())
        data = dict(id='butter-200g', name='Manteiga', category='Laticínios',
            image_url='https://example.org/image.png', package_display='Pote 200g',
            package_net_value=200, package_unit='g', kcal_per_package=1400, price_avg='12.90',
            linked_ingredient_slug='butter', image_credit='Wikimedia / Test',
            image_credit_url='https://example.org/credit')
        await conn.execute("INSERT INTO grocery_markets(id,name,logo_url,website_url) VALUES('test','Teste','','')")
        market = await conn.fetchrow("SELECT * FROM grocery_markets WHERE id='test'")
        assert market['cart_type'] == 'VTEX' and market['cart_url_template'] is None
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE grocery_markets SET cart_type='INVALID'")
        payload = GroceryImport(cards=[data])
        await conn.execute(import_sql(payload))
        await conn.execute(import_sql(payload))
        rows = await load_cards(conn, 'Laticínios', 'MANTEIGA')
        assert len(rows) == 1
        assert rows[0]['package_net_value'] == 200
        assert rows[0]['image_credit'] == 'Wikimedia / Test'
        assert rows[0]['image_credit_url'] == 'https://example.org/credit'
        data['package_unit'] = 'ml'
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute(import_sql(GroceryImport(cards=[data])))
        await conn.execute('ROLLBACK')
        assert (await load_cards(conn))[0]['package_unit'] == 'g'
        await conn.execute((ROOT / 'migrations/074_grocery_unified_curation_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/073_grocery_super_urls_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/070_grocery_card_image_credits_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/064_grocery_retail_cards_updated_at_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/063_grocery_retail_nutrition_facts_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/061_grocery_dietary_fiber_rollback.sql').read_text())
        await conn.execute((ROOT / 'migrations/058_grocery_retail_cards_and_markets_rollback.sql').read_text())
        assert await conn.fetchval("SELECT COUNT(*) FROM grocery_ingredients") == 1
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


async def test_single_food_save_derives_units_and_preserves_existing_recipe_metadata():
    """A 900 ml package is entered once; canonical liters and recipe metadata stay internal."""
    from datetime import datetime, timezone
    from services.grocery_retail_admin import save_record
    import json

    parsed = urlparse(DSN)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/braga_privacy_test':
        raise RuntimeError('Somente banco efêmero local braga_privacy_test')
    conn = await asyncpg.connect(DSN)
    schema = 'single_food_' + uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""
            CREATE TABLE grocery_ingredients(slug TEXT PRIMARY KEY,name TEXT,unit TEXT,details JSONB,active BOOLEAN);
            CREATE TABLE nutrition_catalog_settings(id INTEGER,revision INTEGER);
            INSERT INTO nutrition_catalog_settings VALUES(1,1);
            CREATE TABLE grocery_price_cache(food_name TEXT,ingredient_slug TEXT,price_unit TEXT);
        """)
        for migration in ['058_grocery_retail_cards_and_markets.sql', '061_grocery_dietary_fiber.sql',
                          '063_grocery_retail_nutrition_facts.sql', '064_grocery_retail_cards_updated_at.sql',
                          '070_grocery_card_image_credits.sql', '073_grocery_super_urls.sql', '074_grocery_unified_curation.sql']:
            await conn.execute((ROOT / 'migrations' / migration).read_text())
        await conn.execute("INSERT INTO grocery_markets(id,name,logo_url,website_url) VALUES('fonte','Mercado','','')")
        card = dict(id='oleo-900', name='Óleo de Soja', category='Óleos vegetais',
                    package_display='900 ml', package_net_value=900, package_unit='ml',
                    kcal_per_package=7452, price_avg='9.90')
        reference = dict(market_id='fonte', last_updated=datetime.now(timezone.utc).isoformat())
        async with conn.transaction():
            await save_record(conn, 'food', {'card': card, 'reference': reference})
        ingredient = dict(await conn.fetchrow('SELECT * FROM grocery_ingredients'))
        assert ingredient['unit'] == 'L'
        assert json.loads(ingredient['details'])['minimum'] == 1
        assert await conn.fetchval('SELECT package_net_value FROM grocery_retail_cards') == 900
        assert await conn.fetchval('SELECT price FROM grocery_market_prices') == Decimal('9.90')
        async with conn.transaction():
            await save_record(conn, 'food', {'card': {**card, 'id': 'oleo-500', 'package_net_value': 500}})
        assert await conn.fetchval('SELECT COUNT(*) FROM grocery_ingredients') == 1
        metadata = {'aliases': ['óleo culinário'], 'food_ids': ['food_oil'], 'minimum': 50, 'step': 1,
                    'density_g_per_ml': 0.92}
        await conn.execute('UPDATE grocery_ingredients SET details=$1::jsonb', json.dumps(metadata))
        async with conn.transaction():
            await save_record(conn, 'food', {'card': {**card, 'name': 'Óleo de Soja Tradicional',
                                                    'category': 'Mercearia', 'active': False}})
        saved = await conn.fetchrow('SELECT * FROM grocery_ingredients')
        assert saved['name'] == 'Óleo de Soja Tradicional'
        assert saved['active']  # Another package is still active.
        details = json.loads(saved['details'])
        assert all(details[k] == v for k, v in metadata.items())
        assert details['canonical_group_slug'] == 'Mercearia'
        assert await conn.fetchval("SELECT bool_and(category='Mercearia') FROM grocery_retail_cards")
        with pytest.raises(ValueError, match='Peso e volume'):
            async with conn.transaction():
                await save_record(conn, 'food', {'card': {**card, 'package_unit': 'g'}})
        assert await conn.fetchval("SELECT package_unit FROM grocery_retail_cards WHERE id='oleo-900'") == 'ml'
        await conn.execute("DELETE FROM grocery_retail_cards WHERE id='oleo-500'")
        async with conn.transaction():
            await save_record(conn, 'food', {'card': {**card, 'package_unit': 'un', 'package_net_value': 20,
                                                    'package_display': '20 un'}})
        corrected = await conn.fetchrow('SELECT * FROM grocery_ingredients')
        assert corrected['unit'] == 'un'
        corrected_details = json.loads(corrected['details'])
        assert corrected_details['minimum'] == 1
        assert corrected_details['step'] == 1
        assert corrected_details['food_ids'] == metadata['food_ids']
        assert await conn.fetchval("SELECT package_net_value FROM grocery_retail_cards WHERE id='oleo-900'") == 20
        assert await conn.fetchval("SELECT package_unit FROM grocery_retail_cards WHERE id='oleo-900'") == 'un'

        # One atomic food mutation creates ten quotes and an EAN, with no manual price.
        await conn.executemany("INSERT INTO grocery_markets(id,name,logo_url,website_url) VALUES($1,$1,'','')",
                               [(f'm{i}',) for i in range(10)])
        batch_card = {**card, 'id': 'batch', 'package_unit': 'un', 'package_net_value': 20,
                      'package_display': '20 un', 'barcode_ean': '4006381333931'}
        del batch_card['price_avg']
        quotes = [dict(market_id=f'm{i}', price=f'{i+1}.00', product_sku=f'00{i}',
                       product_store_url=f'https://example.org/p/{i}', in_stock=True,
                       last_updated='2026-01-01T00:00:00Z', last_verified_at='2026-01-02T00:00:00Z') for i in range(10)]
        async with conn.transaction():
            await save_record(conn, 'food', {'card': batch_card, 'quotes': quotes})
        assert await conn.fetchval("SELECT COUNT(*) FROM grocery_market_prices WHERE card_id='batch'") == 10
        saved_card = await conn.fetchrow("SELECT * FROM grocery_retail_cards WHERE id='batch'")
        assert saved_card['price_avg'] == Decimal('5.50') and saved_card['barcode_ean'] == '4006381333931'
        # Omissions preserve other markets; empty prices delete only the specified row.
        await conn.execute("UPDATE grocery_markets SET active=FALSE WHERE id='m9'")
        async with conn.transaction():
            await save_record(conn, 'food', {'card': batch_card, 'quotes': [
                dict(market_id='m0', price=None), {**quotes[1], 'price': '10.01'}]})
        assert await conn.fetchval("SELECT COUNT(*) FROM grocery_market_prices WHERE card_id='batch'") == 9
        assert await conn.fetchval("SELECT price_avg FROM grocery_retail_cards WHERE id='batch'") == Decimal('6.50')
        loaded = next(row for row in await load_cards(conn) if row['id'] == 'batch')
        assert loaded['price_avg'] == Decimal('6.50')
        # A database failure on the final quote must roll back card, ingredient and earlier quotes.
        before_card = dict(await conn.fetchrow("SELECT * FROM grocery_retail_cards WHERE id='batch'"))
        before_ingredient = dict(await conn.fetchrow("SELECT * FROM grocery_ingredients"))
        before_quotes = [dict(row) for row in await conn.fetch("SELECT * FROM grocery_market_prices WHERE card_id='batch' ORDER BY market_id")]
        await conn.execute("""CREATE FUNCTION reject_test_quote() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.product_sku='FAIL' THEN RAISE EXCEPTION 'deliberate batch failure'; END IF; RETURN NEW; END $$;
          CREATE TRIGGER reject_test_quote BEFORE INSERT OR UPDATE ON grocery_market_prices FOR EACH ROW EXECUTE FUNCTION reject_test_quote();""")
        with pytest.raises(asyncpg.RaiseError, match='deliberate batch failure'):
            async with conn.transaction():
                await save_record(conn, 'food', {'card': {**batch_card, 'name': 'Nome revertido'},
                    'quotes': [quotes[0], {**quotes[1], 'product_sku': 'FAIL'}]})
        assert dict(await conn.fetchrow("SELECT * FROM grocery_retail_cards WHERE id='batch'")) == before_card
        assert dict(await conn.fetchrow("SELECT * FROM grocery_ingredients")) == before_ingredient
        assert [dict(row) for row in await conn.fetch("SELECT * FROM grocery_market_prices WHERE card_id='batch' ORDER BY market_id")] == before_quotes
        async with conn.transaction():
            await save_record(conn, 'food', {'card': {**batch_card, 'price_avg': '25.00'}, 'quotes': []})
        assert await conn.fetchval("SELECT price_avg FROM grocery_retail_cards WHERE id='batch'") == Decimal('25.00')
        async with conn.transaction():
            await save_record(conn, 'food', {'card': batch_card, 'quotes': [dict(market_id=f'm{i}', price=None) for i in range(10)]})
        assert await conn.fetchval("SELECT price_avg FROM grocery_retail_cards WHERE id='batch'") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM grocery_market_prices WHERE card_id='batch'") == 0
    finally:
        await conn.execute('ROLLBACK')
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
