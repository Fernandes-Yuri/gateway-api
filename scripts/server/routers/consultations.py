"""Consultas médicas familiares (doc 10 §2.1/§2.2).

Rotas extraídas de ``family_api.py`` quando o chat familiar e o E2EE
migraram em definitivo para o microserviço Go ``braga-chat`` (Fase #6).
O domínio de consultas continua no backend Python porque pertence ao
Care OS e ao agendamento por voz, não ao chat.

O push para os cuidadores é best-effort: falha de FCM nunca quebra a
operação de criar/responder a consulta.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api_identity import firebase_uid
from core.devices import load_devices_by_user

router = APIRouter()


class CreateConsultation(BaseModel):
    title: str
    scheduledDate: str  # ISO format string


class AcceptConsultation(BaseModel):
    consultationId: str
    # doc 10 §2.2: antes esses campos eram lidos com hasattr() em um BaseModel
    # stricto — Pydantic descarta extras, então o nome do cuidador nunca era
    # persistido. Agora são parte do contrato.
    caregiverName: Optional[str] = None
    caregiverRelation: Optional[str] = None


class RejectConsultation(BaseModel):
    consultationId: str


class ConfirmVoiceDraft(BaseModel):
    patientId: str
    draftId: str


def validate_consultation_acceptance(consultation, uid, now=None):
    """Regras para um familiar aceitar/recusar uma consulta sugerida (doc 10 §2.1).

    A consulta pertence ao paciente (user_id); quem responde é um cuidador.
    """
    now = now or datetime.now(timezone.utc)
    if consultation['user_id'] == uid:
        raise ValueError('Apenas um familiar pode aceitar ou recusar a consulta.')
    if consultation['status'] == 'ACCEPTED' and consultation['caregiver_id'] == uid:
        return  # Retentativa idempotente após resposta perdida.
    if consultation['status'] != 'SUGGESTED':
        raise ValueError('Esta consulta já foi respondida.')
    if consultation['scheduled_date'] is not None and consultation['scheduled_date'] <= now:
        raise ValueError('Esta consulta já passou do horário.')


class _ConsultationState:
    """Porta das dependências do server (pool, loader legado e push FCM)."""

    db_pool = None
    load_devices = None
    dispatch_push = None


_state = _ConsultationState()


def configure_state(*, db_pool=None, load_devices=None, dispatch_push=None):
    """Chamado por server.py ao subir a aplicação."""
    _state.db_pool = db_pool
    _state.load_devices = load_devices
    _state.dispatch_push = dispatch_push


def _pool():
    value = _state.db_pool
    if value is None:
        raise HTTPException(503, 'Banco indisponível. Tente novamente.')
    return value


async def _notify_consultation(row, uid, event_type, title, body):
    """Push de consulta (doc 10 §2.2). Best-effort: falha nunca quebra a operação.

    Regra de destinatário: o paciente (user_id) é notificado quando um cuidador
    age; os cuidadores ativos são notificados quando o paciente age.
    """
    if not _state.dispatch_push or not _state.load_devices:
        return
    if row['user_id'] == uid:
        async with _pool().acquire() as conn:
            care_rows = await conn.fetch(
                "SELECT caregiver_id FROM family_bindings WHERE patient_id=$1 AND status='ACTIVE'",
                row['user_id'])
        recipients = [r['caregiver_id'] for r in care_rows if r['caregiver_id']]
    else:
        recipients = [row['user_id']]
    if not recipients:
        return
    devices = await load_devices_by_user(_state.db_pool, _state.load_devices)
    for recipient_uid in recipients:
        for dev in devices.get(recipient_uid) or []:
            token = dev.get('token')
            if not token:
                continue
            try:
                await asyncio.to_thread(
                    _state.dispatch_push,
                    token=token,
                    title=title,
                    body=body,
                    data={
                        'type': event_type,
                        'consultationId': str(row['id']),
                        'patientId': row['user_id'],
                        'title': title,
                        'message': body,
                    },
                )
            except Exception as push_err:
                print(f'[Consultation] Falha no push para {recipient_uid}: {push_err}')


@router.post('/api/consultation/create')
async def create_consultation(c: CreateConsultation, uid: str = Depends(firebase_uid)):
    """Usuário agenda uma consulta para si mesmo."""
    async with _pool().acquire() as conn, conn.transaction():
        now = datetime.now(timezone.utc)
        try:
            scheduled = datetime.fromisoformat(c.scheduledDate.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(422, 'Data/hora inválida.')

        consultation_id = uuid.uuid4()
        row = await conn.fetchrow('''INSERT INTO consultations
            (id, user_id, title, scheduled_date, status, created_at)
            VALUES ($1,$2,$3,$4,'SUGGESTED',$5) RETURNING *''',
            consultation_id, uid, c.title, scheduled, now)
    # Avisa os cuidadores ativos de que uma consulta foi sugerida.
    await _notify_consultation(row, uid, 'CONSULTATION_CREATED',
        'Nova consulta sugerida',
        f'Foi sugerida a consulta "{row["title"]}". Toque para acompanhar.')
    return {'id': str(row['id']), 'title': row['title'],
            'scheduledDate': row['scheduled_date'].isoformat(), 'status': row['status']}


@router.post('/api/consultation/accept')
async def accept_consultation(p: AcceptConsultation, uid: str = Depends(firebase_uid)):
    """Cuidador/familiar aceita uma consulta sugerida."""
    async with _pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', p.consultationId)
        if not row:
            raise HTTPException(404, 'Consulta não encontrada.')
        try:
            validate_consultation_acceptance(row, uid)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

        caregiver_name = p.caregiverName or ''
        caregiver_relation = p.caregiverRelation or ''
        updated = await conn.fetchrow("""UPDATE consultations SET status='ACCEPTED',
            caregiver_id=$2, caregiver_name=$3, caregiver_relation=$4 WHERE id=$1 RETURNING *""",
            row['id'], uid, caregiver_name, caregiver_relation)
    await _notify_consultation(updated, uid, 'CONSULTATION_ACCEPTED',
        'Consulta aceita',
        f'{caregiver_name or "Seu familiar"} aceitou a consulta "{updated["title"]}".')
    return {'id': str(updated['id']), 'status': updated['status']}


@router.post('/api/consultation/reject')
async def reject_consultation(p: RejectConsultation, uid: str = Depends(firebase_uid)):
    """Usuário rejeita uma consulta sugerida pelo cuidador."""
    async with _pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', p.consultationId)
        if not row:
            raise HTTPException(404, 'Consulta não encontrada.')
        try:
            validate_consultation_acceptance(row, uid)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

        updated = await conn.fetchrow("""UPDATE consultations SET status='REJECTED' WHERE id=$1 RETURNING *""",
            row['id'])
    await _notify_consultation(updated, uid, 'CONSULTATION_REJECTED',
        'Consulta recusada',
        f'Foi recusada a consulta "{updated["title"]}".')
    return {'id': str(updated['id']), 'status': updated['status']}


@router.post('/api/consultation/complete')
async def complete_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
    """Usuário marca consulta como concluída."""
    async with _pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
        if not row:
            raise HTTPException(404, 'Consulta não encontrada.')
        updated = await conn.fetchrow("""UPDATE consultations SET status='COMPLETED' WHERE id=$1 RETURNING *""",
            row['id'])
    await _notify_consultation(updated, uid, 'CONSULTATION_COMPLETED',
        'Consulta concluída',
        f'A consulta "{updated["title"]}" foi marcada como concluída.')
    return {'id': str(updated['id']), 'status': updated['status']}


@router.post('/api/consultation/cancel')
async def cancel_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
    """Usuário cancela uma consulta agendada."""
    async with _pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
        if not row:
            raise HTTPException(404, 'Consulta não encontrada.')
        updated = await conn.fetchrow("""UPDATE consultations SET status='CANCELLED' WHERE id=$1 RETURNING *""",
            row['id'])
    await _notify_consultation(updated, uid, 'CONSULTATION_CANCELLED',
        'Consulta cancelada',
        f'A consulta "{updated["title"]}" foi cancelada.')
    return {'id': str(updated['id']), 'status': updated['status']}


# --- D51: confirmacao por BOTAO do rascunho de agendamento por voz ---

@router.post('/api/consultation/confirm-voice-draft')
async def confirm_voice_draft(c: ConfirmVoiceDraft, uid: str = Depends(firebase_uid)):
    """Confirma (manualmente, por botao) o rascunho de agendamento por voz.

    O draft e validado contra o par cuidador/paciente — um draftId
    forjado ou de outra conta nao agenda nada.
    """
    from voice_scheduling import confirmar_rascunho_por_id
    async with _pool().acquire() as conn, conn.transaction():
        envelope = await confirmar_rascunho_por_id(conn, uid, c.patientId, c.draftId)
        if envelope.get('acao') != 'CONSULTA_AGENDADA':
            raise HTTPException(409, envelope.get('fala', 'Agendamento expirado.'))
        return {
            'status': 'success',
            'consultationId': envelope['parametros']['id'],
            'title': envelope['parametros']['title'],
            'scheduledDate': envelope['parametros']['scheduledDate'],
        }
