"""Disponibilidade do app: canal oficial exclusivo pela Google Play Store.

As rotas antigas de APK permanecem como resposta 410, sem metadados ou arquivos.
Enquanto a ficha oficial não estiver publicada, não há link de instalação.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


def app_unavailable_response():
    return JSONResponse(status_code=410, content={
        "status": "coming_soon",
        "distribution_channel": "google_play",
        "available": False,
        "message": "Em breve na Google Play Store. A distribuição de APK pelo gateway foi encerrada."
    }, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/app/latest")
async def get_latest_app_release():
    return app_unavailable_response()


@router.get("/api/app/download")
async def download_app_apk():
    return app_unavailable_response()


@router.get("/api/app/version")
async def get_latest_app_version():
    return app_unavailable_response()


@router.post("/api/app/notify-update")
async def broadcast_app_update():
    # Nenhum payload, token ou catálogo pode restabelecer o antigo broadcast OTA.
    return app_unavailable_response()


@router.get("/")
async def serve_landing_index():
    # Não serve uma landing legada que possa voltar a oferecer o APK.
    return {"status": "ok", "service": "BragaSaude Gateway", "app_available": False,
            "app_channel": "google_play", "app_message": "Em breve na Google Play Store."}


class _AppState:
    """Porta do estado global do server (db_pool, FCM). Preenchida por
    main.py no startup; routers leem sem acoplar o monolito."""

    db_pool = None
    messaging = None
    has_fcm = False


_state = _AppState()


def configure_state(*, db_pool=None, messaging=None, has_fcm=False):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.messaging = messaging
    _state.has_fcm = has_fcm


async def notify_published_release(manifest):
    """Compatibilidade interna: não envia avisos ou links do canal aposentado."""
    return {"status": "retired", "sent_count": 0, "failed_count": 0}
