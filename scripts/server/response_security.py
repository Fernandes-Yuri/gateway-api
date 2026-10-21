"""Headers defensivos sem restringir os documentos HTML de distribuição."""
from starlette.datastructures import MutableHeaders


class ResponseSecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def send_secure(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['X-Content-Type-Options'] = 'nosniff'
                headers['X-Frame-Options'] = 'DENY'
                headers['Referrer-Policy'] = 'no-referrer'
                content_type = headers.get('content-type', '').split(';')[0].strip().lower()
                if content_type == 'application/json' or content_type.endswith('+json'):
                    headers['Content-Security-Policy'] = (
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                    )
                if scope.get('path', '').rstrip('/') == '/health':
                    headers['Cache-Control'] = 'no-store'
            await send(message)

        await self.app(scope, receive, send_secure)
