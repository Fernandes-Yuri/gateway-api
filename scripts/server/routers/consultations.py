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
from pydantic import BaseModel, Field

from api_identity import firebase_uid, require_family_access
from core.devices import load_devices_by_user

router = APIRouter()


class CreateConsultation(BaseModel):
    id: Optional[uuid.UUID] = None
    patientId: Optional[str] = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    scheduledDate: str
    caregiverName: Optional[str] = None
    caregiverRelation: Optional[str] = None


class AcceptConsultation(BaseModel):
    consultationId: uuid.UUID
    # doc 10 §2.2: antes esses campos eram lidos com hasattr() em um BaseModel
    # stricto — Pydantic descarta extras, então o nome do cuidador nunca era
    # persistido. Agora são parte do contrato.
    caregiverName: Optional[str] = None
    caregiverRelation: Optional[str] = None


class RejectConsultation(BaseModel):
    consultationId: uuid.UUID


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


def consultation_json(row):
    def iso(value):
        return value.isoformat() if value is not None else None
    return {'id': str(row['id']), 'patientId': row['user_id'], 'title': row['title'],
            'scheduledDate': iso(row['scheduled_date']), 'status': row['status'],
            'createdBy': row.get('created_by') or row['user_id'],
            'caregiverId': row.get('caregiver_id'), 'caregiverName': row.get('caregiver_name'),
            'caregiverRelation': row.get('caregiver_relation'),
            'createdAt': iso(row.get('created_at')), 'updatedAt': iso(row.get('updated_at'))}


async def authorized_consultation(conn, consultation_id, uid):
    row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', consultation_id)
    if row is None:
        raise HTTPException(404, 'Consulta não encontrada.')
    await require_family_access(conn, uid, row['user_id'])
    return row


@router.get('/api/consultations/{patient_id}')
async def list_consultations(patient_id: str, offset: int = 0, uid: str = Depends(firebase_uid)):
    if offset < 0:
        raise HTTPException(422, 'Página inválida.')
    async with _pool().acquire() as conn:
        await require_family_access(conn, uid, patient_id)
        rows = await conn.fetch('SELECT * FROM consultations WHERE user_id=$1 ORDER BY scheduled_date,id LIMIT 100 OFFSET $2', patient_id, offset)
    return [consultation_json(row) for row in rows]


@router.post('/api/consultation/create')
async def create_consultation(c: CreateConsultation, uid: str = Depends(firebase_uid)):
    patient = c.patientId or uid
    try:
        scheduled = datetime.fromisoformat(c.scheduledDate.replace('Z', '+00:00'))
        if scheduled.tzinfo is None or not c.title.strip():
            raise ValueError('invalid')
    except ValueError:
        raise HTTPException(422, 'Informe título e uma data com fuso horário.')
    consultation_id = c.id or uuid.uuid4()
    async with _pool().acquire() as conn, conn.transaction():
        await require_family_access(conn, uid, patient)
        row = await conn.fetchrow("""INSERT INTO consultations
            (id,user_id,title,scheduled_date,status,created_at,created_by,caregiver_id,caregiver_name,caregiver_relation)
            VALUES($1,$2,$3,$4,$5,now(),$6,$7,$8,$9) ON CONFLICT(id) DO NOTHING RETURNING *""",
            consultation_id, patient, c.title.strip(), scheduled,
            'SUGGESTED' if patient == uid else 'ACCEPTED', uid,
            None if patient == uid else uid, c.caregiverName, c.caregiverRelation)
        created = row is not None
        if row is None:
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 AND user_id=$2 AND created_by=$3', consultation_id, patient, uid)
            if row is None:
                raise HTTPException(409, 'Identificador de consulta indisponível.')
    if created:
        await _notify_consultation(row, uid, 'CONSULTATION_CREATED', 'Consulta agendada',
                                   f'Consulta "{row["title"]}" agendada. Toque para ver os detalhes.')
    return consultation_json(row)


async def transition_consultation(consultation_id, uid, target, name=None, relation=None):
    async with _pool().acquire() as conn, conn.transaction():
        row = await authorized_consultation(conn, consultation_id, uid)
        if target in ('ACCEPTED', 'REJECTED'):
            try:
                validate_consultation_acceptance(row, uid)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        elif row['status'] in ('CANCELLED', 'REJECTED', 'COMPLETED') and row['status'] != target:
            raise HTTPException(409, 'Esta consulta já foi encerrada.')
        if row['status'] == target:
            return consultation_json(row)
        if target == 'ACCEPTED':
            updated = await conn.fetchrow("""UPDATE consultations SET status=$2,caregiver_id=$3,
                caregiver_name=$4,caregiver_relation=$5,updated_at=now() WHERE id=$1 RETURNING *""",
                row['id'], target, uid, name or '', relation or '')
        else:
            updated = await conn.fetchrow('UPDATE consultations SET status=$2,updated_at=now() WHERE id=$1 RETURNING *', row['id'], target)
    await _notify_consultation(updated, uid, f'CONSULTATION_{target}', 'Consulta atualizada',
                               f'A consulta "{updated["title"]}" foi atualizada.')
    return consultation_json(updated)


@router.post('/api/consultation/accept')
async def accept_consultation(p: AcceptConsultation, uid: str = Depends(firebase_uid)):
    return await transition_consultation(p.consultationId, uid, 'ACCEPTED', p.caregiverName, p.caregiverRelation)


@router.post('/api/consultation/reject')
async def reject_consultation(p: RejectConsultation, uid: str = Depends(firebase_uid)):
    return await transition_consultation(p.consultationId, uid, 'REJECTED')


@router.post('/api/consultation/complete')
async def complete_consultation(p: AcceptConsultation, uid: str = Depends(firebase_uid)):
    return await transition_consultation(p.consultationId, uid, 'COMPLETED')


@router.post('/api/consultation/cancel')
async def cancel_consultation(p: AcceptConsultation, uid: str = Depends(firebase_uid)):
    return await transition_consultation(p.consultationId, uid, 'CANCELLED')


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
