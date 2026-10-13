"""Router de distribuicao e atualizacao do app (OTA) + landing page.

Extraido do monolito server.py na Fase A passo 3
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /api/app/latest,
/api/app/download, /api/app/version, /api/app/notify-update, / (landing).
"""
import os
import asyncio
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import RELEASES_DIR, PUBLIC_API_BASE_URL
from core.devices import dispatch_push, load_devices_by_user

router = APIRouter()


class BroadcastUpdateRequest(BaseModel):
    # doc 10 §1B.1: versao e OBRIGATORIA — antes havia defaults chumbados em
    # 1.1.1/build 3 e uma chamada sem parametros avisaria a versao errada.
    version_name: str
    version_code: int
    title: Optional[str] = "Nova Atualização do Braga Saúde Disponível!"
    message: Optional[str] = "Atualizamos o Braga Saúde com melhorias no copiloto de voz e maior estabilidade. Toque para atualizar!"
    download_url: Optional[str] = "https://bragasaude.online/api/app/download"


@router.get("/api/app/latest")
async def get_latest_app_release():
    if _state.db_pool is None:
        raise HTTPException(503, "Catálogo de releases indisponível.")
    async with _state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT version_name,version_code,release_type,filename,file_size_bytes,sha256_hash,changelog,min_android_sdk,created_at FROM app_releases WHERE is_active=TRUE ORDER BY version_code DESC LIMIT 1")
    if not row:
        raise HTTPException(404, "Nenhuma release publicada.")
    result = dict(row)
    if result.get('created_at'):
        result['created_at'] = result['created_at'].isoformat()
    result.pop('id', None)
    result['download_url'] = f"{PUBLIC_API_BASE_URL}/api/app/download?version_code={row['version_code']}"
    result['release_contract'] = 2
    return result


@router.get("/api/app/download")
async def download_app_apk(token: Optional[str] = None, version_code: Optional[int] = None):
    """O catálogo escolhe um arquivo imutável, inclusive entre latest/download."""
    row = None
    if _state.db_pool is not None:
        async with _state.db_pool.acquire() as conn:
            if version_code is not None:
                row = await conn.fetchrow("SELECT * FROM app_releases WHERE version_code=$1", version_code)
            else:
                row = await conn.fetchrow("SELECT * FROM app_releases WHERE is_active=TRUE ORDER BY version_code DESC LIMIT 1")
        if row is None:
            raise HTTPException(404, "Release não encontrada.")
        filename = row['filename']
        if Path(filename).name != filename or not filename.endswith('.apk'):
            raise HTTPException(503, "Caminho de release inválido.")
        candidates = [Path(RELEASES_DIR) / filename]
    else:
        if version_code is not None:
            raise HTTPException(503, "Catálogo de releases indisponível.")
        candidates = [Path(RELEASES_DIR) / name for name in ('BragaSaude.apk', 'latest.apk')]
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            if row:
                def valid():
                    digest = hashlib.sha256()
                    with path.open('rb') as file:
                        for block in iter(lambda: file.read(1024 * 1024), b''):
                            digest.update(block)
                    return path.stat().st_size == row['file_size_bytes'] and digest.hexdigest() == row['sha256_hash'].lower()
                if not await asyncio.to_thread(valid):
                    raise HTTPException(503, "Arquivo não corresponde à release publicada.")
            return FileResponse(path, media_type="application/vnd.android.package-archive", filename="BragaSaude.apk")
    return JSONResponse(status_code=404, content={"detail": "APK de release indisponível."})


@router.get("/api/app/version")
async def get_latest_app_version():
    return await get_latest_app_release()


@router.post("/api/app/notify-update")
async def broadcast_app_update(req: BroadcastUpdateRequest, request: Request):
    """Dispara notificacao push de atualizacao para todos os dispositivos registrados.

    Seguranca (doc 10 §1B.3): broadcast e operacao administrativa. Exige um token
    administrativo real (ADMIN_API_TOKEN do env), independentemente do peer — atras
    do tuneel Cloudflare TODA requisicao chega com client.host == "127.0.0.1", entao
    o criterio "chamada local" nunca pode ser usado como autenticacao (AUD-G01).
    """
    admin_token = os.getenv("ADMIN_API_TOKEN", "")
    if not admin_token:
        # Sem token admin configurado: recurso desativado em vez de aberto.
        raise HTTPException(503, "Broadcast administrativo indisponível: ADMIN_API_TOKEN não configurado.")
    supplied = request.headers.get("Authorization", "")
    if supplied != f"Bearer {admin_token}":
        raise HTTPException(401, "Não autorizado.")
    return await _broadcast_app_update(req)


async def _broadcast_app_update(req: BroadcastUpdateRequest):
    # Plano B (doc 10 §1B): le do banco device_push_tokens (a migracao de
    # startup renomeia o JSON, entao ler load_devices() devolvia 0 aparelhos).
    devices_by_user = await load_devices_by_user(_state.db_pool)
    sent_count = 0
    errors = []

    for uid, devices in devices_by_user.items():
        for dev in devices:
            token = dev.get("token")
            if not token:
                continue
            success = await asyncio.to_thread(dispatch_push,
                token=token,
                title=req.title,
                body=req.message,
                # HIBRIDO (correcao da Causa 3): data-only puro nao acorda um app em
                # estado STOPPED (fechado pelo usuario) — o Android so entrega a
                # mensagem de dados se o processo estiver vivo ou a prioridade for
                # alta o suficiente, e em muitas fabricantes nem assim. Mandando
                # TAMBEM a notificacao, o proprio Google Play Services desenha o
                # balao com o link de download; o data continua la para o caso do
                # app estar aberto (deep link pelo onMessageReceived).
                include_notification=True,
                data={
                    "type": "APP_UPDATE",
                    "title": req.title,
                    "message": req.message,
                    "download_url": req.download_url,
                    "version_name": str(req.version_name),
                    "version_code": str(req.version_code)
                },
                messaging=_state.messaging,
                has_fcm=_state.has_fcm,
            )
            if success:
                sent_count += 1
            else:
                errors.append(uid)

    return {
        "status": "update_broadcasted",
        "total_devices": sum(len(devs) for devs in devices_by_user.values()),
        "sent_count": sent_count,
        "failed_count": len(errors),
        "version": req.version_name
    }


# =============================================================
# ONBOARDING / LANDING PAGE STATICS
# =============================================================
# AUD-G(B): path absoluto do homelab (/home/yurifjs/...) — na EC2 nunca
# existe, e a landing page e servida pelo site oficial na Cloudflare. Guardado
# por os.path.isdir (inerte), mas env-configuravel para qualquer provedor.
ONBOARDING_DIR = os.environ.get(
    "ONBOARDING_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "onboarding_site", "public"),
)


@router.get("/")
async def serve_landing_index():
    index_path = os.path.join(ONBOARDING_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return {"status": "ok", "service": "BragaSaude Gateway"}


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
    return await _broadcast_app_update(BroadcastUpdateRequest(
        version_name='v' + manifest.version_name, version_code=manifest.version_code,
        download_url=f"{PUBLIC_API_BASE_URL}/api/app/download?version_code={manifest.version_code}"))
