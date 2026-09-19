"""Authenticated family invitations. The row lock makes acceptance atomic."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from api_identity import firebase_uid, require_owner, require_family_access
from family_contract import validate_acceptance, validate_consultation_acceptance
from security_defense import get_client_ip, sanitize_zero_trust_text


class AcceptInvitation(BaseModel):
    code: str
    caregiverName: str
    caregiverRelation: str


class DeleteFamilyMessage(BaseModel):
    id: str
    patientId: str | None = None
    sentAt: str | None = None


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


def serialize_binding(row):
    result = dict(row)
    result['id'] = str(result['id'])
    for key in ('created_at', 'expires_at'):
        if result.get(key):
            result[key] = result[key].isoformat()
    return result


def serialize_message(row):
    """D47: inclui expires_at/deleted_at para o app aplicar TTL e tombstones."""
    result = dict(row)
    result['id'] = str(result['id'])
    for key in ('sent_at', 'expires_at', 'deleted_at'):
        if result.get(key):
            result[key] = result[key].isoformat()
    if result.get('deleted_at'):
        result['message_text'] = ''
        result['sender_name'] = ''
        result['icon_type'] = 'LOVE'
    return result


def message_lifetime(sent_at: str, now=None):
    """Retries and delayed uploads never extend the original 24-hour lifetime."""
    now = now or datetime.now(timezone.utc)
    try:
        sent = datetime.fromisoformat(sent_at.replace('Z', '+00:00'))
        if sent.tzinfo is None:
            raise ValueError('timezone required')
        sent = sent.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        raise HTTPException(422, 'Horário de envio inválido.')
    if sent > now + timedelta(minutes=5):
        raise HTTPException(422, 'Confira a data e a hora do aparelho.')
    sent = min(sent, now)
    expires = sent + timedelta(hours=24)
    if expires <= now:
        raise HTTPException(410, 'Mensagem expirada.')
    return sent, expires


async def purge_expired_family_messages(pool) -> int:
    """D47: remove definitivamente tudo o que completou 24h (mensagens e tombstones).

    A regra dura de retenção é `expires_at <= now()` — o tombstone de uma exclusão
    também vive no máximo até o expires_at da própria mensagem, de modo que nenhum
    resíduo ultrapassa a janela de 24h. Retorna a quantidade de linhas removidas.
    """
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        result = await conn.execute('DELETE FROM family_messages WHERE expires_at <= now()')
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


def install_family_routes(app, get_pool, binding_model, message_model, rate_limiter=None,
                          load_devices=None, dispatch_push=None):
    def pool():
        value = get_pool()
        if value is None:
            raise HTTPException(503, 'Banco indisponível. Tente novamente.')
        return value

    @app.post('/api/sync/family-binding')
    async def sync_binding(b: binding_model, request: Request, uid: str = Depends(firebase_uid)):
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_binding_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=pool())
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
            # Sanitização Zero-Trust
            caregiver_name = sanitize_zero_trust_text(b.caregiverName, max_length=100, field_name="caregiverName", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
            caregiver_relation = sanitize_zero_trust_text(b.caregiverRelation, max_length=50, field_name="caregiverRelation", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
            row = await conn.fetchrow('''INSERT INTO family_bindings
                (id, patient_id, caregiver_name, caregiver_relation, connection_code, status, created_at, expires_at)
                VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7)
                ON CONFLICT DO NOTHING RETURNING *''', binding_id, uid, caregiver_name,
                caregiver_relation, code, now, now + timedelta(days=7))
            if row is None:
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1', binding_id)
                if not row or row['patient_id'] != uid or row['connection_code'] != code or row['status'] != 'PENDING':
                    raise HTTPException(409, 'Convite indisponível. Gere um novo código.')
            return {'status': 'success', 'id': str(row['id'])}

    @app.post('/api/family/accept')
    async def accept_invitation(p: AcceptInvitation, request: Request, uid: str = Depends(firebase_uid)):
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_accept_ip", client_ip, limit=5, window_seconds=60, ip=client_ip, pool=pool())
        caregiver_name = sanitize_zero_trust_text(p.caregiverName, max_length=100, field_name="caregiverName", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        caregiver_relation = sanitize_zero_trust_text(p.caregiverRelation, max_length=50, field_name="caregiverRelation", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        if not caregiver_name.strip() or not caregiver_relation.strip():
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
            await conn.execute('INSERT INTO profiles(id, full_name) VALUES($1,$2) ON CONFLICT DO NOTHING', uid, caregiver_name)
            updated = await conn.fetchrow("""UPDATE family_bindings SET caregiver_id=$2,
                caregiver_name=$3, caregiver_relation=$4, status='ACTIVE' WHERE id=$1 RETURNING *""",
                row['id'], uid, caregiver_name, caregiver_relation)
        # doc 10 §4.3: o paciente precisa saber que alguém aceitou o convite.
        # Best-effort, fora da transação: falha de push não desfaz o vínculo.
        if dispatch_push and load_devices and updated is not None:
            patient_uid = updated['patient_id']
            devices = load_devices()
            token = (devices.get(patient_uid) or {}).get('token')
            if token:
                try:
                    await asyncio.to_thread(
                        dispatch_push,
                        token=token,
                        title='Familiar conectado',
                        body=f'{caregiver_name} aceitou seu convite e agora acompanha sua saúde.',
                        data={
                            'type': 'FAMILY_BINDING_ACCEPTED',
                            'bindingId': str(updated['id']),
                            'caregiverName': caregiver_name,
                            'patientId': patient_uid,
                        },
                    )
                except Exception as push_err:
                    print(f'[FamilyBinding] Falha no push de aceite para {patient_uid}: {push_err}')
        return serialize_binding(updated)

    @app.get('/api/family/bindings/{user_id}')
    async def bindings(user_id: str, uid: str = Depends(firebase_uid)):
        require_owner(uid, user_id)
        async with pool().acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=now() AND patient_id=$1", uid)
            rows = await conn.fetch('SELECT * FROM family_bindings WHERE patient_id=$1 OR caregiver_id=$1', uid)
            return [serialize_binding(row) for row in rows]

    @app.post('/api/sync/family-message')
    async def message(m: message_model, request: Request, uid: str = Depends(firebase_uid)):
        require_owner(uid, m.senderId)
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_message_ip", client_ip, limit=30, window_seconds=60, ip=client_ip, pool=pool())
        try:
            message_id = uuid.UUID(m.id) if m.id else uuid.uuid4()
        except ValueError:
            raise HTTPException(422, 'Mensagem inválida.')
        sent, expires = message_lifetime(m.sentAt)

        # Zero-Trust sanitization e protecao contra prompt injection
        clean_text = sanitize_zero_trust_text(m.messageText, max_length=500, field_name="messageText", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
        clean_sender = sanitize_zero_trust_text(m.senderName, max_length=100, field_name="senderName", ip=client_ip, rate_limiter=rate_limiter, pool=pool())

        async with pool().acquire() as conn:
            await require_family_access(conn, uid, m.patientId)
            # D47: TTL de 24h por mensagem — nenhuma conversa é armazenada além disso
            row = await conn.fetchrow('''INSERT INTO family_messages
                (id, patient_id, sender_id, sender_name, message_text, icon_type, is_read, sent_at, expires_at)
                VALUES($1,$2,$3,$4,$5,$6,false,$7,$8) ON CONFLICT DO NOTHING RETURNING id''',
                message_id, m.patientId, uid, clean_sender, clean_text, m.iconType, sent, expires)
            if row is None and not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM family_messages WHERE id=$1 AND sender_id=$2 AND patient_id=$3)', message_id, uid, m.patientId):
                raise HTTPException(409, 'Mensagem indisponível.')

            # Plano de notificações (doc 10 §1A): só resolve destinatário quando a
            # linha foi efetivamente criada — retentativa idempotente de sync não
            # re-notifica.
            notify_recipients: list[str] = []
            if row is not None:
                if uid == m.patientId:
                    # Quem escreveu é o próprio paciente: avisa os cuidadores ativos.
                    care_rows = await conn.fetch(
                        "SELECT caregiver_id FROM family_bindings WHERE patient_id=$1 AND status='ACTIVE'",
                        m.patientId)
                    notify_recipients = [r['caregiver_id'] for r in care_rows if r['caregiver_id']]
                else:
                    # Cuidador escreveu: avisa o aparelho do paciente.
                    notify_recipients = [m.patientId]

        # Push fora da transação: falha de FCM nunca pode quebrar o envio da mensagem.
        if row is not None and dispatch_push and load_devices and notify_recipients:
            devices = load_devices()
            for recipient_uid in notify_recipients:
                token = (devices.get(recipient_uid) or {}).get('token')
                if not token:
                    continue
                try:
                    await asyncio.to_thread(
                        dispatch_push,
                        token=token,
                        title=f'Mensagem de {clean_sender}',
                        body=clean_text[:200],
                        data={
                            'type': 'FAMILY_MESSAGE',
                            'messageId': str(message_id),
                            'senderId': uid,
                            'patientId': m.patientId,
                        },
                    )
                except Exception as push_err:
                    print(f'[FamilyMessage] Falha no push para {recipient_uid}: {push_err}')

        return {'status': 'success', 'id': str(message_id)}

    @app.get('/api/family/messages/{patient_id}')
    async def messages(patient_id: str, limit: int = 50, offset: int = 0, uid: str = Depends(firebase_uid)):
        async with pool().acquire() as conn:
            await require_family_access(conn, uid, patient_id)
            # D47: defesa em profundidade — a API nunca devolve mensagem expirada,
            # mesmo que a purga agendada ainda não tenha rodado. Tombstones seguem
            # na resposta para que o outro aparelho aplique a exclusão localmente.
            rows = await conn.fetch('''SELECT * FROM family_messages
                WHERE patient_id=$1 AND expires_at > now()
                ORDER BY sent_at DESC, id DESC LIMIT $2 OFFSET $3''', patient_id, max(1, min(limit, 200)), max(0, offset))
            return [serialize_message(row) for row in rows]

    @app.post('/api/family/message/delete')
    async def delete_message(p: DeleteFamilyMessage, uid: str = Depends(firebase_uid)):
        """D47 (LGPD Art. 18, VI): o remetente apaga a própria mensagem.

        Tombstone imediato (`deleted_at`): o outro aparelho aplica a exclusão no
        próximo sync, e a purga remove o resíduo junto com a expiração natural.
        """
        try:
            message_id = uuid.UUID(p.id)
        except (ValueError, AttributeError):
            raise HTTPException(422, 'Mensagem inválida.')
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT id, patient_id, sender_id FROM family_messages WHERE id=$1 FOR UPDATE', message_id)
            if not row:
                if not p.patientId or not p.sentAt:
                    return {'status': 'success', 'id': str(message_id)}
                await require_family_access(conn, uid, p.patientId)
                try:
                    sent, expires = message_lifetime(p.sentAt)
                except HTTPException as error:
                    if error.status_code == 410:
                        return {'status': 'success', 'id': str(message_id)}
                    raise
                # Reserve the ID before an ambiguous/in-flight upload can recreate content.
                await conn.execute("""INSERT INTO family_messages
                    (id,patient_id,sender_id,sender_name,message_text,icon_type,is_read,sent_at,expires_at,deleted_at)
                    VALUES($1,$2,$3,'','','LOVE',false,$4,$5,now()) ON CONFLICT DO NOTHING""",
                    message_id, p.patientId, uid, sent, expires)
                row = await conn.fetchrow('SELECT id, patient_id, sender_id FROM family_messages WHERE id=$1 FOR UPDATE', message_id)
            if row['sender_id'] != uid:
                raise HTTPException(403, 'Só quem enviou pode apagar a mensagem.')
            await conn.execute("UPDATE family_messages SET deleted_at=COALESCE(deleted_at,now()), message_text='', sender_name='', icon_type='LOVE' WHERE id=$1", message_id)
        return {'status': 'success', 'id': str(message_id)}

    # --- CONSULTAS MÉDICAS ---

    async def _notify_consultation(row, uid, event_type, title, body):
        """Push de consulta (doc 10 §2.2). Best-effort: falha nunca quebra a operação.

        Regra de destinatário: o paciente (user_id) é notificado quando um cuidador
        age; os cuidadores ativos são notificados quando o paciente age.
        """
        if not dispatch_push or not load_devices:
            return
        if row['user_id'] == uid:
            async with pool().acquire() as conn:
                care_rows = await conn.fetch(
                    "SELECT caregiver_id FROM family_bindings WHERE patient_id=$1 AND status='ACTIVE'",
                    row['user_id'])
            recipients = [r['caregiver_id'] for r in care_rows if r['caregiver_id']]
        else:
            recipients = [row['user_id']]
        if not recipients:
            return
        devices = load_devices()
        for recipient_uid in recipients:
            token = (devices.get(recipient_uid) or {}).get('token')
            if not token:
                continue
            try:
                await asyncio.to_thread(
                    dispatch_push,
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

    @app.post('/api/consultation/create')
    async def create_consultation(c: CreateConsultation, uid: str = Depends(firebase_uid)):
        """Usuário agenda uma consulta para si mesmo."""
        async with pool().acquire() as conn, conn.transaction():
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

            caregiver_name = p.caregiverName or ''
            caregiver_relation = p.caregiverRelation or ''
            updated = await conn.fetchrow("""UPDATE consultations SET status='ACCEPTED',
                caregiver_id=$2, caregiver_name=$3, caregiver_relation=$4 WHERE id=$1 RETURNING *""",
                row['id'], uid, caregiver_name, caregiver_relation)
        await _notify_consultation(updated, uid, 'CONSULTATION_ACCEPTED',
            'Consulta aceita',
            f'{caregiver_name or "Seu familiar"} aceitou a consulta "{updated["title"]}".')
        return {'status': 'success', 'id': str(updated['id']), 'status': updated['status']}

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
        await _notify_consultation(updated, uid, 'CONSULTATION_REJECTED',
            'Consulta recusada',
            f'Foi recusada a consulta "{updated["title"]}".')
        return {'status': 'success', 'id': str(updated['id']), 'status': updated['status']}

    @app.post('/api/consultation/complete')
    async def complete_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
        """Usuário marca consulta como concluída."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            updated = await conn.fetchrow("""UPDATE consultations SET status='COMPLETED' WHERE id=$1 RETURNING *""",
                row['id'])
        await _notify_consultation(updated, uid, 'CONSULTATION_COMPLETED',
            'Consulta concluída',
            f'A consulta "{updated["title"]}" foi marcada como concluída.')
        return {'status': 'success', 'id': str(updated['id']), 'status': updated['status']}

    @app.post('/api/consultation/cancel')
    async def cancel_consultation(c: AcceptConsultation, uid: str = Depends(firebase_uid)):
        """Usuário cancela uma consulta agendada."""
        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM consultations WHERE id=$1 FOR UPDATE', c.consultationId)
            if not row:
                raise HTTPException(404, 'Consulta não encontrada.')
            updated = await conn.fetchrow("""UPDATE consultations SET status='CANCELLED' WHERE id=$1 RETURNING *""",
                row['id'])
        await _notify_consultation(updated, uid, 'CONSULTATION_CANCELLED',
            'Consulta cancelada',
            f'A consulta "{updated["title"]}" foi cancelada.')
        return {'status': 'success', 'id': str(updated['id']), 'status': updated['status']}

    # --- D51: confirmacao por BOTAO do rascunho de agendamento por voz ---

    class ConfirmVoiceDraft(BaseModel):
        patientId: str
        draftId: str

    @app.post('/api/consultation/confirm-voice-draft')
    async def confirm_voice_draft(c: ConfirmVoiceDraft, uid: str = Depends(firebase_uid)):
        """Confirma (manualmente, por botao) o rascunho de agendamento por voz.

        O draft e validado contra o par cuidador/paciente — um draftId
        forjado ou de outra conta nao agenda nada.
        """
        from voice_scheduling import confirmar_rascunho_por_id
        async with pool().acquire() as conn, conn.transaction():
            envelope = await confirmar_rascunho_por_id(conn, uid, c.patientId, c.draftId)
            if envelope.get('acao') != 'CONSULTA_AGENDADA':
                raise HTTPException(409, envelope.get('fala', 'Agendamento expirado.'))
            return {
                'status': 'success',
                'consultationId': envelope['parametros']['id'],
                'title': envelope['parametros']['title'],
                'scheduledDate': envelope['parametros']['scheduledDate'],
            }
