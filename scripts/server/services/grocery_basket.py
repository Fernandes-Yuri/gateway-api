"""Compare observed package prices. Never substitute estimated prices for missing quotes."""
from decimal import Decimal
from urllib.parse import quote, urlencode, urlsplit


def public_url(value):
    if not value:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else None


def cart_url(market, items):
    available = [item for item in items if item['available']]
    kind = market['cart_type']
    if kind == 'ASSISTED' or not available or any(not item['product_sku'] for item in available):
        return None
    template = public_url(market.get('cart_url_template'))
    website = public_url(market.get('website_url'))
    if kind == 'SHOPIFY':
        parts = ','.join(f"{quote(item['product_sku'], safe='')}:{item['quantity']}" for item in available)
        if template and '{items}' in template:
            return template.replace('{items}', parts)
        return website.rstrip('/') + '/cart/' + parts if website else None
    parts = '&'.join(urlencode({'sku': item['product_sku'], 'qty': item['quantity'], 'seller': '1'}) for item in available)
    if template and '{items}' in template:
        return template.replace('{items}', parts)
    if kind == 'VTEX' and not market.get('cart_url_template') and website:
        return website.rstrip('/') + '/checkout/cart/add?' + parts
    return None


def compare_basket(markets, cards, quotes, requested):
    names = {card['id']: card['name'] for card in cards}
    prices = {(row['market_id'], row['card_id']): row for row in quotes}
    rankings = []
    for market in markets:
        items = []
        total = Decimal('0')
        for card_id, quantity in requested.items():
            row = prices.get((market['id'], card_id))
            available = row is not None and row['in_stock']
            subtotal = Decimal(str(row['price'])) * quantity if available else None
            if available:
                total += subtotal
            items.append(dict(card_id=card_id, name=names[card_id], quantity=quantity,
                available=available, price=float(row['price']) if available else None,
                subtotal_brl=float(subtotal) if available else None,
                product_sku=row.get('product_sku') if row else None,
                product_store_url=public_url(row.get('product_store_url')) if row else None,
                last_verified_at=row.get('last_verified_at') if row else None))
        missing = [item['card_id'] for item in items if not item['available']]
        rankings.append(dict(market_id=market['id'], market_name=market['name'],
            market_logo_url=market['logo_url'], website_url=market['website_url'],
            cart_type=market['cart_type'], total_brl=float(total), complete=not missing,
            missing_items=missing, missing_items_count=len(missing),
            coverage=(len(items) - len(missing)) / len(items), items=items,
            super_url=cart_url(market, items),
            assisted_links=[dict(card_id=item['card_id'], name=item['name'], quantity=item['quantity'],
                                 url=item['product_store_url']) for item in items
                            if item['available'] and item['product_store_url']]))
    rankings.sort(key=lambda row: (not row['complete'], row['total_brl'], row['market_id']))
    complete = [row for row in rankings if row['complete']]
    highest = max((Decimal(str(row['total_brl'])) for row in complete), default=Decimal('0'))
    for index, row in enumerate(rankings, 1):
        row['rank'] = index
        row['is_best_deal'] = bool(complete) and row is complete[0]
        row['savings_vs_highest_brl'] = float(highest - Decimal(str(row['total_brl']))) if row['complete'] else None
    return dict(total_items=len(requested), market_rankings=rankings,
                best_market=complete[0] if complete else None)


async def load_comparison(conn, requested):
    ids = list(requested)
    cards = await conn.fetch('SELECT id,name FROM grocery_retail_cards WHERE active AND id=ANY($1::text[])', ids)
    if len(cards) != len(ids):
        raise ValueError('Há produtos inexistentes ou inativos na cesta. Atualize o catálogo.')
    markets = await conn.fetch('SELECT * FROM grocery_markets WHERE active ORDER BY id')
    quotes = await conn.fetch('''SELECT p.* FROM grocery_market_prices p
        JOIN grocery_markets m ON m.id=p.market_id
        WHERE m.active AND p.card_id=ANY($1::text[])''', ids)
    return compare_basket([dict(row) for row in markets], cards, [dict(row) for row in quotes], requested)
