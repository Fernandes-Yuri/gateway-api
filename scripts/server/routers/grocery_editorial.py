"""Attach private editorial endpoints using the catalog's existing authorization."""
from asyncpg.exceptions import ForeignKeyViolationError, SerializationError
from fastapi import APIRouter, Depends, HTTPException, Response
from schemas.grocery_editorial import EditionCreate, EditorialMutation, PublishEdition, ChangePublication
from services.grocery_cards import load_cards
from services import grocery_publications as releases
from services.grocery_editorial import (edition_snapshot, create_edition, mutate_edition,
                                       preview_edition, EditorialConflict)


def build_router(get_pool, require_admin):
    router = APIRouter(prefix='/api/admin/grocery/editions', dependencies=[Depends(require_admin)])

    def pool():
        result = get_pool()
        if result is None:
            raise HTTPException(503, 'Edições de compras indisponíveis.')
        return result

    def translate(exc):
        if isinstance(exc, EditorialConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, LookupError):
            return HTTPException(404, str(exc))
        if isinstance(exc, ForeignKeyViolationError):
            return HTTPException(422, 'Produto ou supermercado não encontrado. Atualize o catálogo.')
        return HTTPException(422, str(exc))

    @router.get('')
    async def list_editions():
        async with pool().acquire() as conn:
            rows = await conn.fetch('SELECT * FROM grocery_editions ORDER BY starts_on DESC,created_at DESC LIMIT 104')
        return [dict(row) for row in rows]

    @router.post('')
    async def new_edition(body: EditionCreate):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction():
                    return await create_edition(conn, body)
        except (ValueError, LookupError, ForeignKeyViolationError) as exc:
            raise translate(exc) from exc

    @router.get('/{edition_id}')
    async def get_edition(edition_id: str):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction(readonly=True, isolation='repeatable_read'):
                    return await edition_snapshot(conn, edition_id)
        except LookupError as exc:
            raise translate(exc) from exc

    @router.put('/{edition_id}')
    async def edit_edition(edition_id: str, body: EditorialMutation):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction():
                    return await mutate_edition(conn, edition_id, body)
        except (ValueError, LookupError, ForeignKeyViolationError, EditorialConflict) as exc:
            raise translate(exc) from exc

    @router.get('/{edition_id}/preview')
    async def preview(edition_id: str):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction(readonly=True, isolation='repeatable_read'):
                    snapshot = await edition_snapshot(conn, edition_id)
                    cards = await load_cards(conn, include_inactive=True)
                    markets = await conn.fetch('SELECT * FROM grocery_markets ORDER BY name,id')
                    return preview_edition(snapshot, cards, markets)
        except LookupError as exc:
            raise translate(exc) from exc
    @router.get('/{edition_id}/publications')
    async def publications(edition_id: str):
        async with pool().acquire() as conn:
            async with conn.transaction(readonly=True, isolation='repeatable_read'):
                return await releases.history(conn, edition_id)

    @router.post('/{edition_id}/publications')
    async def publish(edition_id: str, body: PublishEdition):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction(isolation='repeatable_read'):
                    return await releases.publish(conn, edition_id, body)
        except SerializationError as exc:
            raise HTTPException(409, 'Outra publicação foi alterada. Atualize e tente novamente.') from exc
        except (ValueError, LookupError, EditorialConflict) as exc:
            raise translate(exc) from exc

    @router.put('/{edition_id}/publications')
    async def change_publication(edition_id: str, body: ChangePublication):
        try:
            async with pool().acquire() as conn:
                async with conn.transaction(isolation='repeatable_read'):
                    return await releases.change_publication(conn, edition_id, body)
        except SerializationError as exc:
            raise HTTPException(409, 'Outra publicação foi alterada. Atualize e tente novamente.') from exc
        except (ValueError, LookupError, EditorialConflict) as exc:
            raise translate(exc) from exc
    return router


def build_public_router(get_pool):
    router = APIRouter(prefix='/api/catalog/grocery/publications')

    async def read(response, revision_only, planning_days=None):
        response.headers['Cache-Control'] = 'no-store'
        if get_pool() is None:
            raise HTTPException(503, 'Packs publicados indisponíveis.')
        async with get_pool().acquire() as conn:
            async with conn.transaction(readonly=True, isolation='repeatable_read'):
                return await (releases.public_revision(conn) if revision_only else releases.public_feed(conn, planning_days))

    @router.get('')
    async def feed(response: Response, planning_days: int | None = None):
        if planning_days is not None and planning_days not in (7, 14, 30):
            raise HTTPException(422, 'Escolha 7, 14 ou 30 dias.')
        return await read(response, False, planning_days)

    @router.get('/revision')
    async def revision(response: Response):
        return await read(response, True)
    return router
