"""Loopback-only entry point for a tunnel; never exposes other gateway routes.

Uses the existing FastAPI/httpx environment. Run on 127.0.0.1:8081,
and point cloudflared ONLY at that port, not the Android gateway on 8080.
"""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


def create_proxy(transport=None):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def forward(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                return JSONResponse({'detail': 'Formulário muito grande.'}, status_code=413)
            body.extend(chunk)
        allowed_headers = {'content-type', 'origin', 'access-control-request-method', 'access-control-request-headers'}
        headers = {k: v for k, v in request.headers.items() if k in allowed_headers}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False, trust_env=False) as client:
                upstream = await client.request(request.method, 'http://127.0.0.1:8080' + request.url.path,
                                                content=bytes(body), headers=headers)
        except httpx.HTTPError:
            return JSONResponse({'detail': 'Cadastro temporariamente indisponível.'}, status_code=503)
        response_headers = {k: v for k, v in upstream.headers.items()
                            if k in {'content-type', 'retry-after', 'vary'} or k.startswith('access-control-')}
        response_headers['cache-control'] = 'no-store'
        return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)

    app.add_api_route('/api/public/vip-signups', forward, methods=['POST', 'OPTIONS'])
    app.add_api_route('/api/public/vip-signups/health', forward, methods=['GET', 'OPTIONS'])
    return app


app = create_proxy()
