"""Entrega direcionada: somente o titular pode notificar um vínculo ativo."""
import asyncio
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from api_identity import firebase_uid, require_owner


class CaregiverAlert(BaseModel):
    patientId: str
    caregiverId: str
    message: str = Field(min_length=1, max_length=1500)


def install_caregiver_alert_routes(app, get_pool, load_devices, dispatch_push):
    @app.post('/api/family/health-alert')
    async def health_alert(req: CaregiverAlert, uid: str = Depends(firebase_uid)):
        require_owner(uid, req.patientId)
        pool = get_pool()
        if pool is None:
            raise HTTPException(503, 'Banco indisponível; alerta não enviado.')
        async with pool.acquire() as conn:
            allowed = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM family_bindings WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE')",
                uid, req.caregiverId)
        if not allowed:
            raise HTTPException(403, 'Vínculo de acompanhamento indisponível.')
        device = load_devices().get(req.caregiverId)
        if not device or not device.get('token'):
            raise HTTPException(409, 'O acompanhante não tem dispositivo registrado.')
        sent = await asyncio.to_thread(dispatch_push, token=device['token'],
            title='Aviso de cuidado familiar', body=req.message,
            data={'type': 'HEALTH_ALERT', 'sourceUserId': uid})
        if not sent:
            raise HTTPException(502, 'O serviço de notificações não aceitou o alerta.')
        # Aceitação pelo FCM não comprova leitura ou recebimento no dispositivo.
        return {'status': 'accepted', 'recipients': 1}
