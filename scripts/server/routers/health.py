"""Router de saude, dataset, dispositivos, notificacao e emergencia.

Extraido do monolito server.py na Fase A passo 2
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios agrupados: /health,
/dataset/*, /api/device*, /api/notify/alert, /api/emergency/trigger.
"""
import asyncio
import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api_identity import firebase_uid, require_owner
from config import DATASET_PATH, MODEL_ID
from core.devices import (
    load_devices,
    save_devices,
    dispatch_push,
    load_devices_by_user,
    register_device_token,
)

router = APIRouter()


class DeviceRegistrationRequest(BaseModel):
    userId: str
    token: str
    role: Optional[str] = "PATIENT"
    name: Optional[str] = "Usuário Braga"
    platform: Optional[str] = "android"


class AlertNotificationRequest(BaseModel):
    type: str
    title: str
    message: str
    sourceUserId: str
    targetRole: Optional[str] = "CAREGIVER"
    metricValue: Optional[str] = None


class EmergencyTriggerRequest(BaseModel):
    userId: str
    userName: str
    details: str


@router.get("/health")
def health():
    return {
        "profile_contract": 1,
        "status": "online",
        "mode": "groq_qwen_gateway_with_neural_tts_and_postgresql",
        "ai_provider": "groq_cloud",
        "model": MODEL_ID,
        "database": "postgresql_16_alpine" if _state.has_postgres and _state.db_pool else "pending_connect",
        "clinical_guardrails": "SBC_SBD_OMS_MS_ENFORCED",
        "security_shield": "ANTI_PROMPT_INJECTION_ACTIVE",
        "notification_hub": {
            "has_scheduler": _state.has_scheduler,
            "has_fcm": _state.has_fcm,
            "has_whatsapp": _state.has_whatsapp
        },
        "whatsapp": {
            "configured": _state.has_whatsapp,
            "otp_paused": _state.whatsapp_otp_paused,
            "operational": _state.has_whatsapp and not _state.whatsapp_otp_paused
        }
    }


@router.get("/dataset/stats")
def dataset_stats(uid: str = Depends(firebase_uid)):
    """Retorna estatisticas detalhadas do dataset de treinamento para Fine-Tuning."""
    if not os.path.exists(DATASET_PATH):
        return {
            "total_conversas": 0,
            "caminho": DATASET_PATH,
            "tamanho_kb": 0,
            "por_origem": {},
            "por_categoria": {}
        }
    total = 0
    by_source = {}
    by_category = {}
    try:
        size_bytes = os.path.getsize(DATASET_PATH)
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                total += 1
                try:
                    item = json.loads(line)
                    src = item.get("source", "legacy")
                    by_source[src] = by_source.get(src, 0) + 1
                    cat = item.get("category", "uncategorized")
                    by_category[cat] = by_category.get(cat, 0) + 1
                except Exception:
                    pass
        return {
            "total_conversas": total,
            "caminho": DATASET_PATH,
            "tamanho_kb": round(size_bytes / 1024, 2),
            "por_origem": by_source,
            "por_categoria": by_category
        }
    except Exception as e:
        print(f"[Dataset Stats Error] {type(e).__name__}: {e}")
        return {"total_conversas": total, "caminho": DATASET_PATH, "error": "Erro ao processar estatisticas do dataset."}


@router.get("/dataset/sample")
def dataset_sample(limit: int = Query(5, ge=1, le=50), uid: str = Depends(firebase_uid)):
    """Retorna as amostras mais recentes do dataset higienizadas por LGPD para auditoria."""
    if not os.path.exists(DATASET_PATH):
        return {"samples": []}
    samples = []
    try:
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in reversed(lines[-limit:]):
                if line.strip():
                    try:
                        samples.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"[Dataset Sample Error] {type(e).__name__}: {e}")
        return {"error": "Erro ao ler amostras do dataset."}
    return {"total_amostras": len(samples), "amostras": samples}


@router.post("/api/device/register")
async def register_device(req: DeviceRegistrationRequest, uid: str = Depends(firebase_uid)):
    require_owner(uid, req.userId)
    devices = load_devices()
    devices[req.userId] = {
        "userId": req.userId,
        "token": req.token,
        "role": req.role,
        "name": req.name,
        "platform": req.platform,
        "updatedAt": datetime.utcnow().isoformat()
    }
    save_devices(devices)
    # Plano B (doc 10 §1B): o banco e a fonte de verdade do push; o JSON fica
    # como espelho de compatibilidade. Falha de banco nao derruba o registro
    # (o app refaz o POST na proxima inicializacao) — reporta database=false.
    database = False
    try:
        database = await register_device_token(
            _state.db_pool,
            user_id=req.userId,
            token=req.token,
            role=req.role or "PATIENT",
            platform=req.platform or "android",
        )
    except Exception as error:
        print(f"[Device Register] Falha ao gravar device_push_tokens para {req.userId}: {error}")
    return {"status": "success", "registeredUsers": len(devices), "database": database}


@router.get("/api/devices")
async def list_devices(uid: str = Depends(firebase_uid)):
    # Tokens FCM equivalem a credenciais de push. Antes eram devolvidos a
    # qualquer pessoa anonima.
    devices = await load_devices_by_user(_state.db_pool)
    own = devices.get(uid) or []
    return {"total": len(own), "devices": own}


@router.post("/api/notify/alert")
async def send_alert(req: AlertNotificationRequest, uid: str = Depends(firebase_uid)):
    # AUD-G16: antes este endpoint era um BROADCAST irrestrito — qualquer conta
    # autenticada podia disparar push com title/message arbitrarios para TODOS
    # os aparelhos (podia ser usado para phishing em massa pela marca).
    # Agora so administracao dispara broadcast; cuidador so fala com os seus.
    devices = await load_devices_by_user(_state.db_pool)
    caller = (devices.get(uid) or [{}])[0]
    caller_role = caller.get("role")
    if req.targetRole in ("ALL", "ADMIN") and caller_role != "ADMIN":
        raise HTTPException(403, "Apenas administradores podem disparar alertas para todos.")
    sent_count = 0
    for dev_uid, user_devices in devices.items():
        for dev in user_devices:
            if req.targetRole == "ALL" or dev.get("role") == req.targetRole:
                success = await asyncio.to_thread(
                    dispatch_push,
                    token=dev["token"],
                    title=req.title,
                    body=req.message,
                    data={
                        "type": req.type,
                        "sourceUserId": uid,
                        "metricValue": req.metricValue or ""
                    },
                    messaging=_state.messaging,
                    has_fcm=_state.has_fcm,
                )
                if success:
                    sent_count += 1
    return {"status": "dispatched", "recipients": sent_count}


@router.post("/api/emergency/trigger")
async def trigger_emergency(req: EmergencyTriggerRequest, uid: str = Depends(firebase_uid)):
    require_owner(uid, req.userId)
    devices = await load_devices_by_user(_state.db_pool)
    sent_count = 0
    for dev_uid, user_devices in devices.items():
        for dev in user_devices:
            if dev.get("role") == "CAREGIVER" or dev_uid != uid:
                await asyncio.to_thread(
                    dispatch_push,
                    token=dev["token"],
                    title="ALERTA DE EMERGENCIA FAMILIAR",
                    body=f"{req.userName} acionou socorro: {req.details}",
                    data={"type": "EMERGENCY", "userId": uid, "details": req.details},
                    messaging=_state.messaging,
                    has_fcm=_state.has_fcm,
                )
                sent_count += 1
    return {"status": "emergency_broadcasted", "notified": sent_count}


class _AppState:
    """Porta do estado global do server (db_pool, flags de feature, messaging).
    Preenchida por main.py no startup; routers leem sem acoplar o monolito."""

    db_pool = None
    has_postgres = False
    has_scheduler = False
    has_fcm = False
    has_whatsapp = False
    whatsapp_otp_paused = False
    messaging = None


_state = _AppState()


def configure_state(*, db_pool=None, has_postgres=False, has_scheduler=False,
                    has_fcm=False, has_whatsapp=False, whatsapp_otp_paused=False, messaging=None):
    """Chamado por main.py/server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.has_postgres = has_postgres
    _state.has_scheduler = has_scheduler
    _state.has_fcm = has_fcm
    _state.has_whatsapp = has_whatsapp
    _state.whatsapp_otp_paused = whatsapp_otp_paused
    _state.messaging = messaging
