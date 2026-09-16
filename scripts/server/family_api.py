"""Authenticated family invitations. The row lock makes acceptance atomic."""
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from api_identity import firebase_uid, require_owner, require_family_access
from family_contract import validate_acceptance


class AcceptInvitation(BaseModel):
    code: str
    caregiverName: str
    caregiverRelation: str


class CreateConsultation(BaseModel):
    title: str
    scheduledDate: str  # ISO format string


class AcceptConsultation(BaseModel):
    consultationId: str


class RejectConsultation(BaseModel):
    consultationId: str


def serialize_binding(row):
    result = dict(row)
    result['id'] = str(result['id'])
    for key in ('created_at', 'expires_at'):
        if result.get(key):
            result[key] = result[key].isoformat()
    return result


def install_family_routes(app, get_pool, binding_model, message_model):
    def pool():
        value = get_pool()
        if value is None:
            raise HTTPException(503, 'Banco indisponível. Tente novamente.')
        return value

    @app.post('/api/sync/family-binding')
    async def sync_binding(b: binding_model, uid: str = Depends(firebase_uid)):
        async with pool().acquire() as conn, conn.transaction():
            if b.status == 'REVOKED':
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1 FOR UPDATE', uuid.UUID(b.id))
                if not row or uid not in (row['patient_id'], row['caregiver_id']):
                    raise HTTPException(403, 'Vínculo indisponível.')
                await conn.execute("UPDATE family_bindings SET status='REVOKED' WHERE id=$1", row['id'])
                return {'status': 'success', 'id': str(row['id'])}
            require_owner(uid, b.patientId)
            if b.status != 'PENDING' or b.caregiverId:
                raise HTTPException(422, 'Use o convite para conectar o acompanhante.')
            code = b.connectionCode.strip().upper()
            if len(code) != 8 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in code):
                raise HTTPException(422, 'Código inválido.')
            if not await conn.fetchval('SELECT self_care_complete FROM profiles WHERE id=$1', uid):
                raise HTTPException(409, 'Conclua e sincronize seu perfil antes de criar um convite.')
            binding_id = uuid.UUID(b.id) if b.id else uuid.uuid4()
            now = datetime.now(timezone.utc)
            row = await conn.fetchrow('''INSERT INTO family_bindings
                (id, patient_id, caregiver_name, caregiver_relation, connection_code, status, created_at, expires_at)
                VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7)
                ON CONFLICT DO NOTHING RETURNING *''', binding_id, uid, b.caregiverName,
                b.caregiverRelation, code, now, now + timedelta(days=7))
            if row is None:
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1', binding_id)
                if not row or row['patient_id'] != uid or row['connection_code'] != code or row['status'] != 'PENDING':
                    raise HTTPException(409, 'Convite indisponível. Gere um novo código.')
            return {'status': 'success', 'id': str(row['id'])}

    @app.post('/api/family/accept')
    async def accept_invitation(p: AcceptInvitation, uid: str = Depends(firebase_uid)):
        if not p.caregiverName.strip() or not p.caregiverRelation.strip():
            raise HTTPException(422, 'Informe nome e relação com o familiar.')
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM family_bindings WHERE connection_code=$1 FOR UPDATE', p.code.strip().upper())
            if not row:
                raise HTTPException(404, 'Código indisponível ou expirado.')
            try:
                validate_acceptance(row, uid)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            # Auth account exists before profile enrollment. Create only the FK stub;
            # completing enrollment remains a separate profile operation.
            await conn.execute('INSERT INTO profiles(id, full_name) VALUES($1,$2) ON CONFLICT DO NOTHING', uid, p.caregiverName.strip())
            updated = await conn.fetchrow("""UPDATE family_bindings SET caregiver_id=$2,
                caregiver_name=$3, caregiver_relation=$4, status='ACTIVE' WHERE id=$1 RETURNING *""",
                row['id'], uid, p.caregiverName.strip(), p.caregiverRelation.strip())
            return serialize_binding(updated)

    @app.get('/api/family/bindings/{user_id}')
    async def bindings(user_id: str, uid: str = Depends(firebase_uid)):
        require_owner(uid, user_id)
        async with pool().acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=now() AND patient_id=$1", uid)
            rows = await conn.fetch('SELECT * FROM family_bindings WHERE patient_id=$1 OR caregiver_id=$1', uid)
            return [serialize_binding(row) for row in rows]

    @app.post('/api/sync/family-message')
    async def message(m: message_model, uid: str = Depends(firebase_uid)):
        require_owner(uid, m.senderId)
        message_id = uuid.UUID(m.id) if m.id else uuid.uuid4()
        async with pool().acquire() as conn:
            await require_family_access(conn, uid, m.patientId)
            row = await conn.fetchrow('''INSERT INTO family_messages
                (id, patient_id, sender_id, sender_name, message_text, icon_type, is_read, sent_at)
                VALUES($1,$2,$3,$4,$5,$6,false,now()) ON CONFLICT DO NOTHING RETURNING id''',
                message_id, m.patientId, uid, m.senderName, m.messageText, m.iconType)
            if row is None and not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM family_messages WHERE id=$1 AND sender_id=$2 AND patient_id=$3)', message_id, uid, m.patientId):
                raise HTTPException(409, 'Mensagem indisponível.')
        return {'status': 'success', 'id': str(message_id)}

    @app.get('/api/family/messages/{patient_id}')
    async def messages(patient_id: str, limit: int = 50, uid: str = Depends(firebase_uid)):
        async with pool().acquire() as conn:
            await require_family_access(conn, uid, patient_id)
            rows = await conn.fetch('SELECT * FROM family_messages WHERE patient_id=$1 ORDER BY sent_at DESC LIMIT $2', patient_id, max(1, min(limit, 200)))
            return [dict(row, id=str(row['id']), sent_at=row['sent_at'].isoformat()) for row in rows]

    # --- CONSULTAS MÉDICAS ---

    @app.post('/api/consultation/create')
    async def create_consultation(c: CreateConsultation, uid: str = Depends(firebase_uid)):
        """Usuário agenda uma consulta para si mesmo."""
        async with pool().acquire() as conn, conn.transaction():
            now = datetime.now(timezone.utc)
            import re
            # Parse the scheduled date
            try:
                scheduled = datetime.fromisoformat(c.scheduledDate.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(422, 'Data/hora inválida.')
            
            consultation_id = uuid.uuid4()
            row = await conn.fetchrow('''INSERT INTO consultations
                (id, user_id, title, scheduled_date, status, created_at)
                VALUES ($1,$2,$3,$4,'SUGGESTED',$5) RETURNING *''',
                consultation_id, uid, c.title, scheduled, now)
            return {'status': 'success', 'id': str(row['id']), 'title': row['title'], 'scheduledDate': row['scheduled_date'].isoformat(), 'status': row['status']}

    @app.post('/api/consultation/accept')
    async def accept_consultation(p: AcceptConsultation, uid: str = Depends(firebase_uid)):
        """Cuidador/familiar aceita uma consulta sugerida."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', p.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            try:
                validate_consultation_acceptance(row, uid)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            
            updated = await conn.fetchrow("""UPDATE consultations SET status='ACCEPTED',
                caregiver_id=$2, caregiver_name=$3, caregiver_relation=$4 WHERE id=$1 RETURNING *""",
                row['id'], uid, p.caregiverName if hasattr(p, 'caregiverName') else '', p.caregiverRelation if hasattr(p, 'caregiverRelation') else '')
            return {'status': 'success', 'id': str(row['id']), 'status': row['status']}

    @app.post('/api/consultation/reject')
    async def reject_consultation(p: RejectConsultation, uid: str = Depends(firebase_uid)):
        """Usuário rejeita uma consulta sugerida pelo cuidador."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', p.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            try:
                validate_consultation_acceptance(row, uid)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            
            updated = await conn.fetchrow("""UPDATE consultations SET status='REJECTED' WHERE id=$1 RETURNING *""",
                row['id'])
            return {'status': 'success', 'id': str(row['id']), 'status': row['status']}

    @app.post('/api/consultation/complete')
    async def complete_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
        """Usuário marca consulta como concluída."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            updated = await conn.fetchrow("""UPDATE consultations SET status='COMPLETED' WHERE id=$1 RETURNING *""",
                row['id'])
            return {'status': 'success', 'id': str(row['id']), 'status': row['status']}

    @app.post('/api/consultation/cancel')
    async def cancel_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
        """Usuário cancela uma consulta agendada."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            updated = await conn.fetchrow("""UPDATE consultations SET status='CANCELLED' WHERE id=$1 RETURNING *""",
                row['id'])
            return {'status': 'success', 'id': str(row['id']), 'status': row['status']}
