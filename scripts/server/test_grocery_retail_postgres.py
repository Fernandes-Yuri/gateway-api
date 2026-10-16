"""Retail migration and editorial imports against the existing ephemeral CI database."""
import os
import uuid
from pathlib import Path
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
        data = dict(id='butter-200g', name='Manteiga', category='Laticínios',
            image_url='https://example.org/image.png', package_display='Pote 200g',
            package_net_value=200, package_unit='g', kcal_per_package=1400, price_avg='12.90',
            linked_ingredient_slug='butter')
        payload = GroceryImport(cards=[data])
        await conn.execute(import_sql(payload))
        await conn.execute(import_sql(payload))
        rows = await load_cards(conn, 'Laticínios', 'MANTEIGA')
        assert len(rows) == 1
        assert rows[0]['package_net_value'] == 200
        data['package_unit'] = 'ml'
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute(import_sql(GroceryImport(cards=[data])))
        await conn.execute('ROLLBACK')
        assert (await load_cards(conn))[0]['package_unit'] == 'g'
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
