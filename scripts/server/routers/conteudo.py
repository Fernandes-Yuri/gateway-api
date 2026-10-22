"""Consulta autenticada dos snapshots de conteúdo público das clínicas.

Sem rota pública: estratégia, hashes e corpos ficam atrás do painel
administrativo, no mesmo modelo de autorização do editorial.
"""
import json

from fastapi import APIRouter, Depends, Query, Request

from routers import admin

router = APIRouter()
_URL_MAX = 2048
_LIMITE_PADRAO = 50


def _pool():
    # _require_pool já responde 503 quando não há banco configurado.
    return admin._require_pool()


@router.get('/api/admin/conteudo/status', dependencies=[Depends(admin.require_service_token)])
async def status(request: Request):
    admin._portal_identity(request)
    pool = _pool()
    async with pool.acquire() as conn:
        linhas = await conn.fetch(
            "SELECT url, hash_sha256, match_state, http_status, tamanho, coletado_em "
            "FROM (SELECT DISTINCT ON (url) * FROM conteudo_snapshots ORDER BY url, coletado_em DESC) u "
            "ORDER BY url")
    return {
        'recursos': [
            {
                'url': r['url'],
                'hash_sha256': r['hash_sha256'],
                'match_state': r['match_state'],
                'http_status': r['http_status'],
                'tamanho': r['tamanho'],
                'coletado_em': r['coletado_em'].isoformat(),
            } for r in linhas
        ],
    }


@router.get('/api/admin/conteudo/snapshots', dependencies=[Depends(admin.require_service_token)])
async def snapshots(request: Request, url: str = Query(min_length=8, max_length=_URL_MAX),
                    limite: int = Query(default=_LIMITE_PADRAO, ge=1, le=200)):
    admin._portal_identity(request)
    pool = _pool()
    async with pool.acquire() as conn:
        linhas = await conn.fetch(
            'SELECT id,url,hash_sha256,match_state,http_status,tamanho,headers,coletado_em '
            'FROM conteudo_snapshots WHERE url=$1 ORDER BY coletado_em DESC LIMIT $2', url, limite)
    return {
        'snapshots': [
            {
                'id': r['id'],
                'url': r['url'],
                'hash_sha256': r['hash_sha256'],
                'match_state': r['match_state'],
                'http_status': r['http_status'],
                'tamanho': r['tamanho'],
                'headers': json.loads(r['headers']),
                'coletado_em': r['coletado_em'].isoformat(),
            } for r in linhas
        ],
    }
