"""Retail snapshots with observed reference prices and their sources."""
import json


async def load_cards(conn, category=None, q=None, *, include_inactive=False):
    rows = await conn.fetch(f"""SELECT * FROM grocery_retail_cards WHERE {"TRUE" if include_inactive else "active"}
        AND ($1::text IS NULL OR category=$1)
        AND ($2::text IS NULL OR strpos(lower(name),lower($2))>0)
        ORDER BY category,name,id""", category, q)
    quotes = await conn.fetch(f"""SELECT p.*,m.name AS market_name,m.logo_url AS market_logo_url,m.website_url
        FROM grocery_market_prices p JOIN grocery_markets m ON m.id=p.market_id
        WHERE {"TRUE" if include_inactive else "m.active"} AND p.card_id=ANY($1::text[]) ORDER BY p.last_updated DESC,m.id""", [r['id'] for r in rows])
    by_card = {}
    for quote in quotes:
        by_card.setdefault(quote['card_id'], []).append(dict(quote))
    result = []
    for row in rows:
        card = dict(row)
        facts = card.get('nutrition_facts')
        if isinstance(facts, str):
            facts = json.loads(facts)
        if facts is None:
            facts = [
                {'key': 'calories', 'label': 'Calorias', 'unit': 'kcal', 'value': card.get('kcal_per_package', 0)},
                {'key': 'carbohydrates', 'label': 'Carboidratos', 'unit': 'g', 'value': card.get('carbs_g', 0)},
                {'key': 'protein', 'label': 'Proteínas', 'unit': 'g', 'value': card.get('protein_g', 0)},
                {'key': 'fat', 'label': 'Gorduras', 'unit': 'g', 'value': card.get('fat_g', 0)},
            ]
            if card.get('fiber_g') is not None:
                facts.append({'key': 'fiber', 'label': 'Fibras alimentares', 'unit': 'g', 'value': card['fiber_g']})
        card['nutrition_facts'] = facts
        card['markets'] = by_card.get(card['id'], [])
        result.append(card)
    return result
