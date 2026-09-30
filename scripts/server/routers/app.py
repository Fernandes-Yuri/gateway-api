"""Router de distribuicao e atualizacao do app (OTA) + landing page.

Extraido do monolito server.py na Fase A passo 3
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /api/app/latest,
/api/app/download, /api/app/version, /api/app/notify-update, / (landing).
"""
import os
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
    """Retorna metadados da ultima versao disponivel para Auto-Update."""
    default_payload = {
        "version_name": "1.0",
        "version_code": 1,
        "release_type": "release",
        "filename": "braga-saude.apk",
        "download_url": "https://github.com/Fernandes-Yuri/bragasaude-releases/releases/download/v1.0.0/app-release.apk",
        "changelog": "Versão inicial de testes do Braga Saúde.",
        "min_android_sdk": 26
    }
    db_pool = _state.db_pool
    if not db_pool:
        return default_payload
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT version_name, version_code, release_type, filename, file_size_bytes,
                       sha256_hash, download_url, changelog, min_android_sdk, created_at
                FROM app_releases
                WHERE is_active = TRUE
                ORDER BY version_code DESC
                LIMIT 1
            """)
            if not row:
                return default_payload
            return {
                "version_name": row["version_name"],
                "version_code": row["version_code"],
                "release_type": row["release_type"],
                "filename": row["filename"],
                "file_size_bytes": row["file_size_bytes"],
                "sha256_hash": row["sha256_hash"],
                "download_url": row["download_url"] or f"{PUBLIC_API_BASE_URL}/api/app/download",
                "changelog": row["changelog"],
                "min_android_sdk": row["min_android_sdk"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None
            }
    except Exception as e:
        print(f"[App Release Error] {e}")
        return default_payload


@router.get("/api/app/download")
async def download_app_apk(token: Optional[str] = None):
    """Endpoint de download do APK oficial da Release."""
    candidates = [
        os.path.join(RELEASES_DIR, "BragaSaude.apk"),
        os.path.join(RELEASES_DIR, "latest.apk"),
        "/home/ec2-user/braga-gateway/releases/BragaSaude.apk",
        "/home/ec2-user/braga-gateway/releases/latest.apk",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "releases", "BragaSaude.apk"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "releases", "latest.apk"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "releases", "BragaSaude.apk"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "releases", "latest.apk"),
    ]
    for apk_path in candidates:
        if os.path.exists(apk_path) and os.path.getsize(apk_path) > 0:
            return FileResponse(
                apk_path,
                media_type="application/vnd.android.package-archive",
                filename="BragaSaude.apk"
            )
    return JSONResponse(
        status_code=404,
        content={"detail": "APK de release em preparação. Tente novamente em alguns instantes."}
    )


@router.get("/api/app/version")
async def get_latest_app_version():
    """Retorna os metadados da versao mais recente ativa do aplicativo."""
    db_pool = _state.db_pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT version_name, version_code, release_type, filename, file_size_bytes, sha256_hash, changelog, min_android_sdk "
                    "FROM app_releases WHERE is_active = TRUE ORDER BY version_code DESC LIMIT 1"
                )
                if row:
                    return {
                        "version_name": row["version_name"],
                        "version_code": row["version_code"],
                        "release_type": row["release_type"],
                        "file_size_bytes": row["file_size_bytes"],
                        "sha256_hash": row["sha256_hash"],
                        "changelog": row["changelog"],
                        "min_android_sdk": row["min_android_sdk"],
                        "download_url": "https://bragasaude.online/api/app/download"
                    }
        except Exception as e:
            print(f"[Version Lookup Error] {e}")

    return {
        "version_name": "1.1.1",
        "version_code": 3,
        "release_type": "release",
        "file_size_bytes": 13941419,
        "sha256_hash": "4C624FABB031F92FB7A64C3D0F91B6EC40253152630F853080CC2BC252645702",
        "changelog": "Versão oficial de produção: eliminação total de com.example, foco estrito de voz em sinais vitais com confirmação manual, auto-update push e vinculação de WhatsApp com validação real OTP.",
        "download_url": "https://bragasaude.online/api/app/download"
    }


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
            success = dispatch_push(
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
# existe, e a landing page e servida pela PWA/site na Cloudflare. Guardado
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
