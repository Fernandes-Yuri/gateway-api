"""Consistent retail snapshots and comparison of complete baskets only."""
from decimal import Decimal
import json
from schemas.grocery_cards import BasketComparisonResult, MarketBasketTotal


async def load_cards(conn, category=None, q=None, *, include_inactive=False):
    rows = await conn.fetch(f"""SELECT * FROM grocery_retail_cards WHERE {"TRUE" if include_inactive else "active"}
        AND ($1::text IS NULL OR category=$1)
        AND ($2::text IS NULL OR strpos(lower(name),lower($2))>0)
        ORDER BY category,name,id""", category, q)
    quotes = await conn.fetch(f"""SELECT p.*,m.name AS market_name,m.logo_url AS market_logo_url,m.website_url
        FROM grocery_market_prices p JOIN grocery_markets m ON m.id=p.market_id
        WHERE {"TRUE" if include_inactive else "m.active"} AND p.card_id=ANY($1::text[]) ORDER BY m.name""", [r['id'] for r in rows])
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


def compare_basket(items, cards, markets):
    quantities = {}
    for item in items:
        quantities[item.card_id] = quantities.get(item.card_id, 0) + item.quantity
        if quantities[item.card_id] > 999:
            raise ValueError('Quantidade máxima por produto: 999.')
    by_id = {c['id']: c for c in cards}
    if quantities.keys() - by_id.keys():
        raise ValueError('A cesta contém produtos indisponíveis. Atualize o catálogo.')
    if not quantities:
        return BasketComparisonResult(total_items=0, total_planned_kcal=0)
    rankings = []
    for market in markets:
        total, missing = Decimal('0'), 0
        for card_id, quantity in quantities.items():
            quote = next((p for p in by_id[card_id]['markets'] if p['market_id'] == market['id']), None)
            if quote is None:
                missing += 1
            else:
                total += Decimal(str(quote['price'])) * quantity
        rankings.append(MarketBasketTotal(market_id=market['id'], market_name=market['name'],
            market_logo_url=market['logo_url'], website_url=market['website_url'],
            total_brl=total.quantize(Decimal('.01')), missing_items_count=missing))
    rankings.sort(key=lambda r: (r.missing_items_count > 0, r.missing_items_count, r.total_brl, r.market_id))
    complete = [r for r in rankings if not r.missing_items_count]
    best = complete[0] if complete else None
    if best:
        highest = max(r.total_brl for r in complete)
        for row in complete:
            row.is_best_deal = row.total_brl == best.total_brl
            row.savings_vs_highest_brl = highest - row.total_brl
    return BasketComparisonResult(total_items=sum(quantities.values()),
        total_planned_kcal=sum((Decimal(str(by_id[k]['kcal_per_package'])) * n for k, n in quantities.items()), Decimal('0')),
        best_market=best, market_rankings=rankings)
