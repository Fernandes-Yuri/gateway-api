"""CMS das clínicas. Autorização, publicação e validação são do gateway."""
import base64
import binascii
import json
import re
from html import escape
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from routers import admin
from portal_auth_security import auth_limit

router = APIRouter()
CATEGORIES = ['Saúde mental', 'Família e cuidado', 'Ansiedade e depressão',
              'Álcool e outras drogas', 'Apostas e jogos', 'Direitos e acesso ao cuidado']


def safe_url(value):
    if not value:
        return ''
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.hostname == 'localhost' or '.' not in url.hostname):
        raise ValueError('Use uma URL pública HTTPS, sem credenciais.')
    return value


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=2, max_length=200)
    url: str = Field(min_length=8, max_length=2048)
    _url = field_validator('url')(safe_url)


class Article(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=8, max_length=150)
    slug: str = Field(min_length=3, max_length=120, pattern=r'^[a-z0-9]+(-[a-z0-9]+)*$')
    description: str = Field(min_length=30, max_length=300)
    category: str
    kind: Literal['Article', 'NewsArticle'] = 'Article'
    body: str = Field(min_length=100, max_length=60000)
    author_name: str = Field(min_length=2, max_length=120)
    author_bio: str = Field(min_length=10, max_length=600)
    reviewer_name: str = Field(default='', max_length=120)
    reviewer_credentials: str = Field(default='', max_length=200)
    cover_url: str = Field(default='', max_length=2048)
    cover_alt: str = Field(default='', max_length=250)
    cover_credit: str = Field(default='', max_length=250)
    sources: list[Source] = Field(default_factory=list, max_length=30)
    reviewed: bool = False

    _cover = field_validator('cover_url')(safe_url)

    @field_validator('slug')
    @classmethod
    def available_slug(cls, value):
        if value == 'politica-editorial':
            raise ValueError('Esse endereço é reservado pelo site.')
        return value

    @field_validator('category')
    @classmethod
    def category_allowed(cls, value):
        if value not in CATEGORIES:
            raise ValueError('Escolha uma categoria editorial disponível.')
        return value


class Save(BaseModel):
    model_config = ConfigDict(extra='forbid')
    article: Article
    version: int = Field(ge=0)


class Transition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    action: Literal['publish', 'archive']


class Member(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=254)
    role: Literal['writer', 'publisher']
    enabled: bool = True


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def private(row):
    data = dict(row)
    data['id'] = str(data['id'])
    data['draft'] = decode(data['draft'])
    data['published'] = decode(data['published']) if data['published'] else None
    return data


async def identity(request):
    email = admin._portal_identity(request)
    pool = admin._require_pool()
    await auth_limit(pool, 'editorial', email, 90, 60)
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT a.role AS admin_role,m.role,m.enabled
            FROM admin_users a LEFT JOIN editorial_members m ON m.email=a.email WHERE a.email=$1''', email)
    if not row:
        raise HTTPException(403, 'Conta não autorizada.')
    # Ser admin do portal, sozinho, não permite publicar conteúdo.
    role = 'publisher' if row['admin_role'] == 'superadmin' else row['role'] if row['enabled'] else None
    return {'email': email, 'role': role, 'manage_members': row['admin_role'] == 'superadmin'}


def require_editor(user):
    if user['role'] not in ('writer', 'publisher'):
        raise HTTPException(403, 'Solicite autorização editorial ao responsável pela marca.')


def require_owner(row, user):
    require_editor(user)
    if not row or (user['role'] != 'publisher' and row['owner_email'] != user['email']):
        raise HTTPException(404, 'Artigo não encontrado.')


@router.get('/api/admin/editorial/session', dependencies=[Depends(admin.require_service_token)])
async def session(request: Request):
    user = await identity(request)
    return {**user, 'categories': CATEGORIES, 'can_write': user['role'] in ('writer', 'publisher'),
            'can_publish': user['role'] == 'publisher', 'body_format': 'paragraphs-headings-lists',
            'limits': {'title': 150, 'description': 300, 'body': 60000}}


@router.get('/api/admin/editorial/articles', dependencies=[Depends(admin.require_service_token)])
async def articles(request: Request):
    user = await identity(request)
    require_editor(user)
    async with admin._require_pool().acquire() as conn:
        rows = await conn.fetch('''SELECT * FROM editorial_articles
            WHERE ($1::boolean OR owner_email=$2) ORDER BY updated_at DESC LIMIT 100''',
                                user['role'] == 'publisher', user['email'])
    return {'items': [private(row) for row in rows]}


@router.put('/api/admin/editorial/articles/{article_id}', dependencies=[Depends(admin.require_service_token)])
async def save(article_id: UUID, body: Save, request: Request):
    user = await identity(request)
    require_editor(user)
    data = body.article.model_dump()
    async with admin._require_pool().acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', str(article_id))
        row = await conn.fetchrow('SELECT * FROM editorial_articles WHERE id=$1 FOR UPDATE', article_id)
        if row:
            require_owner(row, user)
            if row['version'] != body.version:
                raise HTTPException(409, 'Outra edição foi salva. Recarregue antes de continuar.')
            if row['published_at'] and row['slug'] != data['slug']:
                raise HTTPException(422, 'O endereço não pode mudar depois da primeira publicação.')
        elif body.version != 0:
            raise HTTPException(404, 'Artigo não encontrado.')
        conflict = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM editorial_articles WHERE slug=$1 AND id<>$2)', data['slug'], article_id)
        if conflict:
            raise HTTPException(409, 'Esse endereço já é usado por outro artigo.')
        # Serialização da disputa por slug mesmo entre IDs diferentes.
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', data['slug'])
        conflict = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM editorial_articles WHERE slug=$1 AND id<>$2)', data['slug'], article_id)
        if conflict:
            raise HTTPException(409, 'Esse endereço já é usado por outro artigo.')
        updated = await conn.fetchrow('''INSERT INTO editorial_articles(id,owner_email,slug,draft)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(id) DO UPDATE
            SET slug=EXCLUDED.slug,draft=EXCLUDED.draft,version=editorial_articles.version+1,updated_at=NOW()
            RETURNING *''', article_id, user['email'], data['slug'], json.dumps(data))
        await conn.execute('INSERT INTO editorial_events(article_id,actor_email,action,version) VALUES($1,$2,$3,$4)',
                           article_id, user['email'], 'save', updated['version'])
    return private(updated)


@router.post('/api/admin/editorial/articles/{article_id}', dependencies=[Depends(admin.require_service_token)])
async def transition(article_id: UUID, body: Transition, request: Request):
    user = await identity(request)
    if user['role'] != 'publisher':
        raise HTTPException(403, 'Somente publicadores podem publicar ou retirar artigos.')
    async with admin._require_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM editorial_articles WHERE id=$1 FOR UPDATE', article_id)
        require_owner(row, user)
        if body.version != row['version']:
            raise HTTPException(409, 'Versão desatualizada. Recarregue o artigo.')
        article = Article.model_validate(decode(row['draft']))
        if body.action == 'publish':
            if not article.reviewed or not article.sources:
                raise HTTPException(422, 'Confirme a revisão editorial e informe ao menos uma fonte antes de publicar.')
            if article.cover_url and (not article.cover_alt or not article.cover_credit):
                raise HTTPException(422, 'Informe descrição e crédito da imagem de capa.')
            if bool(article.reviewer_name) != bool(article.reviewer_credentials):
                raise HTTPException(422, 'Informe nome e credenciais da revisão técnica, ou deixe ambos vazios.')
        updated = await conn.fetchrow('''UPDATE editorial_articles SET
            published=CASE WHEN $2='publish' THEN draft ELSE published END,
            state=CASE WHEN $2='publish' THEN 'published' ELSE 'archived' END,
            published_at=CASE WHEN $2='publish' THEN COALESCE(published_at,NOW()) ELSE published_at END,
            updated_at=NOW(),version=version+1 WHERE id=$1 RETURNING *''', article_id, body.action)
        await conn.execute('''INSERT INTO editorial_events(article_id,actor_email,action,version,snapshot)
            VALUES($1,$2,$3,$4,$5::jsonb)''', article_id, user['email'], body.action,
                           updated['version'], json.dumps(article.model_dump()))
    return private(updated)


@router.get('/api/admin/editorial/members', dependencies=[Depends(admin.require_service_token)])
async def members(request: Request):
    user = await identity(request)
    if not user['manage_members']:
        raise HTTPException(403, 'Somente o responsável pode administrar acessos.')
    async with admin._require_pool().acquire() as conn:
        rows = await conn.fetch('SELECT email,role,enabled FROM editorial_members ORDER BY email')
    return {'items': [dict(row) for row in rows]}


@router.put('/api/admin/editorial/members', dependencies=[Depends(admin.require_service_token)])
async def grant(body: Member, request: Request):
    user = await identity(request)
    if not user['manage_members']:
        raise HTTPException(403, 'Somente o responsável pode administrar acessos.')
    email = body.email.strip().lower()
    async with admin._require_pool().acquire() as conn, conn.transaction():
        exists = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM admin_users WHERE email=$1)', email)
        if not exists:
            raise HTTPException(422, 'Cadastre a conta autorizada no portal primeiro.')
        await conn.execute('''INSERT INTO editorial_members(email,role,enabled) VALUES($1,$2,$3)
            ON CONFLICT(email) DO UPDATE SET role=EXCLUDED.role,enabled=EXCLUDED.enabled,updated_at=NOW()''', email, body.role, body.enabled)
        await conn.execute('INSERT INTO editorial_events(actor_email,action,snapshot) VALUES($1,$2,$3::jsonb)',
                           user['email'], 'membership', json.dumps(body.model_dump()))
    return {'success': True}


def public(row):
    return {**decode(row['published']), 'id': str(row['id']),
            'published_at': row['published_at'], 'modified_at': row['published_modified_at']}


# Nunca retornar draft, email de acesso, permissões ou auditoria na API pública.
PUBLIC_SELECT = '''SELECT id,published,published_at,
    COALESCE((SELECT MAX(e.created_at) FROM editorial_events e
      WHERE e.article_id=editorial_articles.id AND e.action='publish'),published_at) AS published_modified_at
    FROM editorial_articles WHERE state='published' AND published IS NOT NULL'''


@router.get('/api/editorial/articles')
async def public_articles(response: Response, page: int = Query(1, ge=1, le=10000), limit: int = Query(12, ge=1, le=100)):
    async with admin._require_pool().acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM editorial_articles WHERE state='published' AND published IS NOT NULL")
        rows = await conn.fetch(PUBLIC_SELECT + ' ORDER BY published_at DESC,id LIMIT $1 OFFSET $2', limit, (page - 1) * limit)
    response.headers['Cache-Control'] = 'public, max-age=0, s-maxage=60'
    return {'items': [public(row) for row in rows], 'total': total, 'page': page, 'limit': limit}


@router.get('/api/editorial/articles/{slug}')
async def public_article(slug: str, response: Response):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug) or len(slug) > 120:
        raise HTTPException(404, 'Artigo não encontrado.')
    async with admin._require_pool().acquire() as conn:
        row = await conn.fetchrow(PUBLIC_SELECT + ' AND slug=$1', slug)
    if not row:
        raise HTTPException(404, 'Artigo não encontrado.')
    response.headers['Cache-Control'] = 'public, max-age=0, s-maxage=60'
    return public(row)


class Media(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data: str = Field(min_length=12, max_length=2800000)
    content_type: Literal['image/jpeg', 'image/png', 'image/webp']


@router.post('/api/admin/editorial/articles/{article_id}/media', dependencies=[Depends(admin.require_service_token)])
async def upload_media(article_id: UUID, body: Media, request: Request):
    user = await identity(request)
    require_editor(user)
    try:
        data = base64.b64decode(body.data, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Imagem inválida.')
    signatures = {'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                  'image/jpeg': data.startswith(b'\xff\xd8\xff'),
                  'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
    if not signatures[body.content_type] or len(data) > 2097152:
        raise HTTPException(422, 'Envie PNG, JPEG ou WebP de até 2 MB.')
    media_id = uuid4()
    async with admin._require_pool().acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM editorial_articles WHERE id=$1', article_id)
        require_owner(row, user)
        await conn.execute('INSERT INTO editorial_media(id,article_id,content_type,data) VALUES($1,$2,$3,$4)',
                           media_id, article_id, body.content_type, data)
    return {'url': f'https://api.bragasaude.online/api/editorial/media/{media_id}'}


@router.get('/api/editorial/media/{media_id}')
async def public_media(media_id: UUID):
    url = f'https://api.bragasaude.online/api/editorial/media/{media_id}'
    async with admin._require_pool().acquire() as conn:
        row = await conn.fetchrow("""SELECT m.content_type,m.data FROM editorial_media m
            JOIN editorial_articles a ON a.id=m.article_id
            WHERE m.id=$1 AND a.state='published' AND a.published->>'cover_url'=$2""", media_id, url)
    if not row:
        raise HTTPException(404, 'Imagem não encontrada.')
    return Response(content=row['data'], media_type=row['content_type'],
                    headers={'Cache-Control': 'public, max-age=0, s-maxage=60', 'X-Content-Type-Options': 'nosniff'})


PUBLIC_SITE = 'https://clinicas.bragasaude.online'
SITEMAP_PAGE_SIZE = 10000
STATIC_PATHS = ['', '/sobre', '/privacidade', '/termos', '/cookies', '/noticias', '/noticias/politica-editorial']


def xml_url(path, modified=None):
    lastmod = f'<lastmod>{escape(modified.isoformat())}</lastmod>' if modified else ''
    return f'<url><loc>{PUBLIC_SITE}{escape(path)}</loc>{lastmod}</url>'


@router.get('/api/editorial/sitemap.xml')
async def sitemap(page: int | None = Query(None, ge=1, le=100000)):
    """URLs do frontend, apenas de versões publicadas, com índice ao crescer."""
    async with admin._require_pool().acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM editorial_articles WHERE state='published' AND published IS NOT NULL")
        pages = max(1, (total + SITEMAP_PAGE_SIZE - 1) // SITEMAP_PAGE_SIZE)
        if page is None and pages > 1:
            urls = ''.join(f'<sitemap><loc>https://api.bragasaude.online/api/editorial/sitemap.xml?page={i}</loc></sitemap>'
                           for i in range(1, pages + 1))
            root = f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</sitemapindex>'
        else:
            selected = page or 1
            if selected > pages:
                raise HTTPException(404, 'Página de sitemap não encontrada.')
            rows = await conn.fetch("""SELECT slug,COALESCE((SELECT MAX(e.created_at) FROM editorial_events e
                WHERE e.article_id=editorial_articles.id AND e.action='publish'),published_at) AS modified
                FROM editorial_articles WHERE state='published' AND published IS NOT NULL
                ORDER BY published_at DESC,id LIMIT $1 OFFSET $2""", SITEMAP_PAGE_SIZE, (selected - 1) * SITEMAP_PAGE_SIZE)
            urls = ''.join(xml_url(path) for path in STATIC_PATHS) if selected == 1 else ''
            urls += ''.join(xml_url('/noticias/' + row['slug'], row['modified']) for row in rows)
            root = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response('<?xml version="1.0" encoding="UTF-8"?>' + root, media_type='application/xml',
                    headers={'Cache-Control': 'public, max-age=0, s-maxage=60'})


class PublicEditorialCORS:
    """Somente leitura pública, sem cookies e sem ampliar CORS administrativo."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/editorial/'):
            return await self.app(scope, receive, send)
        if scope['method'] == 'OPTIONS':
            response = Response(status_code=204, headers={
                'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
                'Access-Control-Allow-Headers': 'Accept', 'Access-Control-Max-Age': '86400'})
            return await response(scope, receive, send)

        async def public_send(message):
            if message['type'] == 'http.response.start':
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in (b'access-control-allow-origin', b'access-control-allow-credentials')]
                headers.extend([(b'access-control-allow-origin', b'*'), (b'x-content-type-options', b'nosniff')])
                message = {**message, 'headers': headers}
            await send(message)
        return await self.app(scope, receive, public_send)
