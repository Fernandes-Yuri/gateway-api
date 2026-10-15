"""Validate an editorial JSON and emit transactional SQL for the existing migration/DB workflow.

Usage: python scripts/server/seed_grocery_cards.py catalog.json > /tmp/grocery.sql
No database access or embedded credentials. Review the SQL before applying it.
Omitted entries are preserved; use active=false to retire cards/markets.
"""
import argparse
import json
from pathlib import Path
from schemas.grocery_cards import GroceryImport


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, list):
        return 'ARRAY[' + ','.join(literal(v) for v in value) + ']::text[]'
    return "'" + str(value).replace("'", "''") + "'"


def import_sql(payload):
    statements = ['BEGIN;', "SET LOCAL lock_timeout = '10s';", 'SET LOCAL standard_conforming_strings = on;']
    for table, rows, keys in [('grocery_markets', payload.markets, ['id']),
                               ('grocery_retail_cards', payload.cards, ['id']),
                               ('grocery_market_prices', payload.prices, ['card_id', 'market_id'])]:
        seen = set()
        for row in rows:
            data = row.model_dump()
            identity = tuple(data[k] for k in keys)
            if identity in seen:
                raise ValueError(f'Duplicate {table}: {identity}')
            seen.add(identity)
            columns = list(data)
            statements.append(f"INSERT INTO {table} ({','.join(columns)}) VALUES (" +
                ','.join(literal(data[c]) for c in columns) + ') ON CONFLICT (' + ','.join(keys) +
                ') DO UPDATE SET ' + ','.join(f'{c}=EXCLUDED.{c}' for c in columns if c not in keys) + ';')
    statements.append("""DO $grocery$
BEGIN
 IF EXISTS (SELECT 1 FROM grocery_retail_cards c
   JOIN grocery_ingredients i ON i.slug=c.linked_ingredient_slug
   WHERE (i.unit,c.package_unit) NOT IN (('kg','g'),('L','ml'),('un','un'))) THEN
   RAISE EXCEPTION 'A embalagem e o ingrediente devem ter a mesma dimensão';
 END IF;
END $grocery$;""")
    return '\n'.join(statements + ['COMMIT;'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    args = parser.parse_args()
    print(import_sql(GroceryImport(**json.loads(args.catalog.read_text(encoding='utf-8')))))
