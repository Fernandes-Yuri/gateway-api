"""Immutable editorial releases, atomic heads, and private release history."""
import json
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from services.grocery_editorial import edition_snapshot, preview_edition, EditorialConflict
from services.grocery_cards import load_cards


def today():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).date()


async def history(conn, edition_id):
    current = await conn.fetchval('SELECT publication_id FROM grocery_publication_heads WHERE edition_id=$1', edition_id)
    rows = await conn.fetch("""SELECT id,version,source_revision,published_at,starts_on,ends_on,
        jsonb_array_length(snapshot->'packs') AS pack_count FROM grocery_publications
        WHERE edition_id=$1 ORDER BY version DESC""", edition_id)
    return dict(current_publication_id=current, publications=[dict(r) for r in rows])


async def locked_head(conn, edition_id, expected_head):
    # All publishers/restorers take these locks in the same order.
    exists = await conn.fetchval('SELECT id FROM grocery_editions WHERE id=$1 FOR UPDATE', edition_id)
    if exists is None:
        raise LookupError('Edição não encontrada.')
    await conn.fetchval('SELECT revision FROM grocery_publication_state WHERE id=1 FOR UPDATE')
    await conn.execute('INSERT INTO grocery_publication_heads(edition_id) VALUES($1) ON CONFLICT DO NOTHING', edition_id)
    head = await conn.fetchval('SELECT publication_id FROM grocery_publication_heads WHERE edition_id=$1', edition_id)
    if head != expected_head:
        raise EditorialConflict('A publicação vigente mudou. Atualize o histórico antes de continuar.')
    return head


def freeze_preview(preview, pack_ids):
    by_id = {p['pack']['id']: p for p in preview['packs']}
    if set(pack_ids) - by_id.keys():
        raise ValueError('Há packs que não pertencem a esta edição.')
    selected = [by_id[id] for id in pack_ids]
    if not selected or any(not p['ready_for_review'] for p in selected):
        raise ValueError('Resolva as pendências dos packs selecionados antes de publicar.')
    edition = {key: preview['edition'][key] for key in ('id', 'title', 'starts_on', 'ends_on', 'region')}
    # Editorial notes, unfinished packs and the working revision never enter the public body.
    return dict(edition=edition, packs=selected, is_preview=False)


async def publish(conn, edition_id, body):
    request_id = str(body.request_id)
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
    previous = await conn.fetchrow('SELECT id,edition_id,source_revision,request_fingerprint FROM grocery_publications WHERE id=$1', request_id)
    if previous:
        if previous['edition_id'] != edition_id or previous['source_revision'] != body.expected_revision or previous['request_fingerprint'] != fingerprint:
            raise EditorialConflict('Identificador de publicação já utilizado em outra operação.')
        return await history(conn, edition_id)
    await locked_head(conn, edition_id, body.expected_publication_id)
    # Recheck after the lock to make retries idempotent.
    if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM grocery_publications WHERE id=$1)', request_id):
        return await history(conn, edition_id)
    snapshot = await edition_snapshot(conn, edition_id)
    if snapshot['edition']['revision'] != body.expected_revision:
        raise EditorialConflict('O rascunho mudou. Gere e confira uma nova prévia antes de publicar.')
    cards = await load_cards(conn, include_inactive=True)
    markets = await conn.fetch('SELECT * FROM grocery_markets ORDER BY name,id')
    preview = preview_edition(snapshot, cards, markets)
    if preview['review_digest'] != body.review_digest:
        raise EditorialConflict('O catálogo ou as pesquisas mudaram desde a prévia. Revise novamente.')
    if snapshot['edition']['ends_on'] < today():
        raise ValueError('A edição está vencida. Ajuste a validade no rascunho antes de publicar.')
    frozen = freeze_preview(preview, body.pack_ids)
    version = await conn.fetchval('UPDATE grocery_publication_state SET revision=revision+1 WHERE id=1 RETURNING revision')
    await conn.execute("""INSERT INTO grocery_publications(id,version,edition_id,source_revision,starts_on,ends_on,snapshot,request_fingerprint)
        VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""", request_id, version, edition_id, body.expected_revision,
        snapshot['edition']['starts_on'], snapshot['edition']['ends_on'], json.dumps(frozen, default=str, ensure_ascii=False), fingerprint)
    await conn.execute('UPDATE grocery_publication_heads SET publication_id=$2 WHERE edition_id=$1', edition_id, request_id)
    return await history(conn, edition_id)


async def change_publication(conn, edition_id, body):
    await locked_head(conn, edition_id, body.expected_publication_id)
    publication = await conn.fetchrow('SELECT id,ends_on FROM grocery_publications WHERE id=$1 AND edition_id=$2', body.publication_id, edition_id)
    if publication is None:
        raise LookupError('Publicação não encontrada nesta edição.')
    if body.action == 'withdraw' and body.publication_id != body.expected_publication_id:
        raise EditorialConflict('Somente a publicação vigente pode ser retirada.')
    if body.action == 'restore' and publication['ends_on'] < today():
        raise ValueError('Esta versão já venceu. Duplique a edição e revise uma nova validade.')
    head = publication['id'] if body.action == 'restore' else None
    if head != body.expected_publication_id:
        await conn.execute('UPDATE grocery_publication_heads SET publication_id=$2 WHERE edition_id=$1', edition_id, head)
        await conn.execute('UPDATE grocery_publication_state SET revision=revision+1 WHERE id=1')
    return await history(conn, edition_id)


async def public_revision(conn):
    revision = await conn.fetchval('SELECT revision FROM grocery_publication_state WHERE id=1')
    return dict(revision=revision, business_date=today().isoformat())


async def public_feed(conn, planning_days=None):
    state = await public_revision(conn)
    rows = await conn.fetch("""SELECT p.id,p.version,p.published_at,p.source_revision,p.snapshot
        FROM grocery_publication_heads h JOIN grocery_publications p ON p.id=h.publication_id
        WHERE p.starts_on<=$1 AND p.ends_on>=$1 ORDER BY p.starts_on DESC,p.version DESC""", today())
    publications = []
    for row in rows:
        frozen = json.loads(row['snapshot']) if isinstance(row['snapshot'], str) else row['snapshot']
        # Normalize old immutable snapshots in the response without rewriting history.
        packs = [dict(p, pack={**p['pack'], 'planning_days': p['pack'].get('planning_days', 7)}) for p in frozen['packs']]
        if planning_days is not None:
            packs = [p for p in packs if p['pack']['planning_days'] == planning_days]
        if packs:
            publications.append(dict(id=row['id'], version=row['version'], published_at=row['published_at'], **{**frozen, 'packs': packs}))
    return dict(**state, planning_period_options=[7, 14, 30], publications=publications)
