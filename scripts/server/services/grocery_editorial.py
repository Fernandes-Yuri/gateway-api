"""Private weekly drafts. No mutation writes public prices or published content."""
from decimal import Decimal
import hashlib
import json
from uuid import uuid4
from pydantic import ValidationError
from schemas.grocery_editorial import EditionFields, EditorialQuote, QuoteKey, PackDraft, PackKey


class EditorialConflict(Exception):
    pass


async def edition_snapshot(conn, edition_id):
    edition = await conn.fetchrow('SELECT * FROM grocery_editions WHERE id=$1', edition_id)
    if edition is None:
        raise LookupError('Edição não encontrada.')
    quotes = await conn.fetch('SELECT * FROM grocery_edition_quotes WHERE edition_id=$1 ORDER BY card_id,market_id', edition_id)
    packs = await conn.fetch('SELECT * FROM grocery_editorial_packs WHERE edition_id=$1 ORDER BY name,id', edition_id)
    items = await conn.fetch("""SELECT i.* FROM grocery_editorial_pack_items i
        JOIN grocery_editorial_packs p ON p.id=i.pack_id WHERE p.edition_id=$1 ORDER BY i.position""", edition_id)
    by_pack = {}
    for item in items:
        by_pack.setdefault(item['pack_id'], []).append(dict(card_id=item['card_id'], quantity=item['quantity']))
    return {'edition': dict(edition), 'quotes': [dict(q) for q in quotes],
            'packs': [dict(p, items=by_pack.get(p['id'], [])) for p in packs]}


async def create_edition(conn, body):
    source = None
    if body.source_edition_id:
        # Serialize duplication against edits, retaining original research timestamps.
        found = await conn.fetchval('SELECT id FROM grocery_editions WHERE id=$1 FOR SHARE', body.source_edition_id)
        if not found:
            raise LookupError('Edição de origem não encontrada.')
        source = await edition_snapshot(conn, body.source_edition_id)
    edition_id = str(uuid4())
    await conn.execute("""INSERT INTO grocery_editions(id,title,starts_on,ends_on,region,notes,source_edition_id)
        VALUES($1,$2,$3,$4,$5,$6,$7)""", edition_id, body.title, body.starts_on, body.ends_on, body.region, body.notes, body.source_edition_id)
    if source:
        await conn.execute("""INSERT INTO grocery_edition_quotes
            (edition_id,card_id,market_id,price,product_store_url,researched_at,confirmed,conditions,minimum_quantity,membership_required)
            SELECT $1,card_id,market_id,price,product_store_url,researched_at,FALSE,conditions,minimum_quantity,membership_required
            FROM grocery_edition_quotes WHERE edition_id=$2""", edition_id, body.source_edition_id)
        for pack in source['packs']:
            new_id = str(uuid4())
            await conn.execute("""INSERT INTO grocery_editorial_packs(id,edition_id,market_id,name,description,cover_url,pack_type,category)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)""", new_id, edition_id, pack['market_id'], pack['name'],
                pack['description'], pack['cover_url'], pack['pack_type'], pack['category'])
            await conn.execute("""INSERT INTO grocery_editorial_pack_items(pack_id,card_id,quantity,position)
                SELECT $1,card_id,quantity,position FROM grocery_editorial_pack_items WHERE pack_id=$2""", new_id, pack['id'])
    return await edition_snapshot(conn, edition_id)


def validated_mutation(body):
    model = {'metadata': EditionFields, 'quote': EditorialQuote, 'remove_quote': QuoteKey,
             'pack': PackDraft, 'remove_pack': PackKey}[body.operation]
    try:
        return model.model_validate(body.payload)
    except ValidationError as exc:
        # Human-readable validation without internal input/context serialization.
        messages = [str(e['msg']).removeprefix('Value error, ') for e in exc.errors()]
        raise ValueError('Revise o cadastro: ' + '; '.join(dict.fromkeys(messages))) from exc


async def mutate_edition(conn, edition_id, body):
    data = validated_mutation(body)
    revision = await conn.fetchval('SELECT revision FROM grocery_editions WHERE id=$1 FOR UPDATE', edition_id)
    if revision is None:
        raise LookupError('Edição não encontrada.')
    if revision != body.expected_revision:
        raise EditorialConflict('A edição mudou em outra janela. Atualize antes de salvar novamente.')
    if body.operation == 'metadata':
        await conn.execute('UPDATE grocery_editions SET title=$2,starts_on=$3,ends_on=$4,region=$5,notes=$6 WHERE id=$1',
            edition_id, data.title, data.starts_on, data.ends_on, data.region, data.notes)
    elif body.operation == 'quote':
        await conn.execute("""INSERT INTO grocery_edition_quotes
            (edition_id,card_id,market_id,price,product_store_url,researched_at,confirmed,conditions,minimum_quantity,membership_required)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(edition_id,card_id,market_id)
            DO UPDATE SET price=EXCLUDED.price,product_store_url=EXCLUDED.product_store_url,researched_at=EXCLUDED.researched_at,
            confirmed=EXCLUDED.confirmed,conditions=EXCLUDED.conditions,minimum_quantity=EXCLUDED.minimum_quantity,
            membership_required=EXCLUDED.membership_required""", edition_id, data.card_id, data.market_id, data.price,
            str(data.product_store_url) if data.product_store_url else None, data.researched_at, data.confirmed,
            data.conditions, data.minimum_quantity, data.membership_required)
    elif body.operation == 'remove_quote':
        await conn.execute('DELETE FROM grocery_edition_quotes WHERE edition_id=$1 AND card_id=$2 AND market_id=$3',
            edition_id, data.card_id, data.market_id)
    elif body.operation == 'remove_pack':
        removed = await conn.fetchval('DELETE FROM grocery_editorial_packs WHERE edition_id=$1 AND id=$2 RETURNING id', edition_id, data.id)
        if not removed:
            raise LookupError('Pack não encontrado nesta edição.')
    elif body.operation == 'pack':
        pack_id = data.id or str(uuid4())
        if data.id:
            existing = await conn.fetchval('SELECT edition_id FROM grocery_editorial_packs WHERE id=$1', data.id)
            if existing != edition_id:
                raise LookupError('Pack não encontrado nesta edição.')
        await conn.execute("""INSERT INTO grocery_editorial_packs(id,edition_id,market_id,name,description,cover_url,pack_type,category)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(id) DO UPDATE SET market_id=EXCLUDED.market_id,
            name=EXCLUDED.name,description=EXCLUDED.description,cover_url=EXCLUDED.cover_url,
            pack_type=EXCLUDED.pack_type,category=EXCLUDED.category""", pack_id, edition_id, data.market_id,
            data.name, data.description, str(data.cover_url) if data.cover_url else None, data.pack_type, data.category)
        await conn.execute('DELETE FROM grocery_editorial_pack_items WHERE pack_id=$1', pack_id)
        if data.items:
            await conn.executemany('INSERT INTO grocery_editorial_pack_items(pack_id,card_id,quantity,position) VALUES($1,$2,$3,$4)',
                [(pack_id, i.card_id, i.quantity, position) for position, i in enumerate(data.items)])
    await conn.execute('UPDATE grocery_editions SET revision=revision+1,updated_at=now() WHERE id=$1', edition_id)
    return await edition_snapshot(conn, edition_id)


def preview_edition(snapshot, cards, markets):
    """Same exact composition in every market; no cheapest-item mixing."""
    by_card = {c['id']: c for c in cards}
    by_market = {m['id']: m for m in markets}
    quotes = {(q['card_id'], q['market_id']): q for q in snapshot['quotes']}
    previews = []
    for pack in snapshot['packs']:
        issues, lines, rankings = [], [], []
        market = by_market.get(pack['market_id'])
        if not market or not market['active']:
            issues.append('O supermercado do pack está inativo ou indisponível.')
        if not pack['items']:
            issues.append('Adicione produtos ao pack.')
        total, kcal = Decimal('0'), Decimal('0')
        for item in pack['items']:
            card = by_card.get(item['card_id'])
            quote = quotes.get((item['card_id'], pack['market_id']))
            name = card['name'] if card else item['card_id']
            if not card or not card['active']:
                issues.append(f'{name}: produto inativo ou indisponível.')
            if card and pack['pack_type'] == 'category' and card['category'] != pack['category']:
                issues.append(f'{name}: não pertence à categoria escolhida.')
            usable = bool(quote and quote['confirmed'] and quote.get('product_store_url') and item['quantity'] >= quote['minimum_quantity'])
            if not usable:
                issues.append(f'{name}: confirme a pesquisa, informe a fonte e confira a quantidade mínima no mercado do pack.')
            subtotal = Decimal(str(quote['price'])) * item['quantity'] if usable else None
            if subtotal is not None:
                total += subtotal
            if card:
                kcal += Decimal(str(card['kcal_per_package'])) * item['quantity']
            lines.append(dict(item, product=card, quote=quote, subtotal_brl=subtotal))
        for candidate in markets:
            if not candidate['active']:
                continue
            subtotal, missing, conditions = Decimal('0'), 0, []
            for item in pack['items']:
                quote = quotes.get((item['card_id'], candidate['id']))
                if not quote or not quote['confirmed'] or not quote.get('product_store_url') or item['quantity'] < quote['minimum_quantity']:
                    missing += 1
                else:
                    subtotal += Decimal(str(quote['price'])) * item['quantity']
                    if quote['membership_required'] or quote['conditions'] or quote['minimum_quantity'] > 1:
                        conditions.append(dict(card_id=item['card_id'], conditions=quote['conditions'],
                            membership_required=quote['membership_required'], minimum_quantity=quote['minimum_quantity']))
            rankings.append(dict(market=candidate, total_brl=subtotal, missing_items_count=missing, conditions=conditions))
        rankings.sort(key=lambda row: (row['missing_items_count'] > 0, row['missing_items_count'], row['total_brl'], row['market']['id']))
        complete = [row for row in rankings if row['missing_items_count'] == 0 and pack['items']]
        lowest = min((row['total_brl'] for row in complete), default=None)
        highest = max((row['total_brl'] for row in complete), default=None)
        for row in rankings:
            row['is_best_deal'] = not issues and lowest is not None and not row['missing_items_count'] and row['total_brl'] == lowest
        chosen_complete = next((row for row in complete if row['market']['id'] == pack['market_id']), None)
        previews.append(dict(pack=pack, market=market, items=lines, issues=issues,
            ready_for_review=not issues, total_brl=total, total_planned_kcal=kcal,
            comparison=rankings, comparison_complete=not issues and chosen_complete is not None and len(complete) >= 2,
            savings_vs_highest_brl=max(Decimal('0'), highest-total) if not issues and highest is not None and chosen_complete else None))
    result = dict(edition=snapshot['edition'], packs=previews, is_preview=True,
        ready_for_review=bool(previews) and all(p['ready_for_review'] for p in previews))
    result['review_digest'] = hashlib.sha256(json.dumps(result, default=str, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
