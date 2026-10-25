"""Página pública de socorro independente dos canais de atendimento."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/socorro", include_in_schema=False)
async def emergency_options_page():
    """Acesso público: não depende de login, vínculo, banco ou modelo."""
    from fastapi.responses import HTMLResponse
    from services.emergency_page import emergency_page
    return HTMLResponse(emergency_page(), headers={
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })

