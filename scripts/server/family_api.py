"""Authenticated family invitations and Blind Relay E2EE messages (SPEC-13)."""
import asyncio
import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from api_identity import firebase_uid, require_owner, require_family_access
from core.devices import load_devices_by_user
from family_contract import validate_acceptance, validate_consultation_acceptance
from security_defense import get_client_ip, sanitize_zero_trust_text

JOKER_CONNECTION_CODE = "TESTFAM1"
JOKER_PATIENT_UID = "patient-demo-testfam"
JOKER_PATIENT_NAME = "Carlos Braga (Paciente Teste)"

E2EE_CURRENT_VERSION = 1
E2EE_GCM_IV_BYTES = 12
E2EE_GCM_TAG_BYTES = 16
E2EE_MAX_CIPHERTEXT_BYTES = 8192
E2EE_PUSH_BODY = "Nova mensagem protegida por criptografia ponta a ponta."


class AcceptInvitation(BaseModel):
    code: str
    caregiverName: str
    caregiverRelation: str
    caregiverPublicKey: Optional[str] = None


class PublishFamilyPublicKey(BaseModel):
    bindingId: Optional[str] = None
    patientId: Optional[str] = None
    publicKey: str


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


def validate_e2ee_public_key(raw_key: Optional[str]) -> Optional[str]:
    """Valida a chave publica ECDH (SubjectPublicKeyInfo Base64) sem chaves privadas no servidor."""
    if raw_key is None:
        return None
    cleaned = raw_key.strip()
    if not cleaned:
        return None
    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, 'Chave pública E2EE inválida.')
    if not (32 <= len(decoded) <= 512):
        raise HTTPException(422, 'Tamanho de chave pública E2EE inválido.')
    return base64.b64encode(decoded).decode('ascii')


def parse_e2ee_envelope(raw_blob: Optional[str], strict: bool = False) -> Optional[str]:
    """SPEC-13 §2.2: valida o envelope E2EE AES-256-GCM sem decifrar o conteúdo (Blind Relay)."""
    if raw_blob is None:
        if strict:
            raise HTTPException(422, 'Payload E2EE ausente.')
        return None
    cleaned = raw_blob.strip()
    if not cleaned:
        if strict:
            raise HTTPException(422, 'Payload E2EE vazio.')
        return None
    if not (cleaned.startswith('{') and cleaned.endswith('}')):
        if strict:
            raise HTTPException(422, 'Payload E2EE malformado.')
        return None
    try:
        envelope = json.loads(cleaned)
    except (ValueError, TypeError):
        if strict:
            raise HTTPException(422, 'JSON de payload E2EE inválido.')
        return None
    if not isinstance(envelope, dict):
        if strict:
            raise HTTPException(422, 'Envelope E2EE deve ser um objeto JSON.')
        return None
    if not strict and not {'v', 'iv', 'ciphertext'}.issubset(envelope.keys()):
        return None
    version = envelope.get('v')
    if type(version) is not int or version != E2EE_CURRENT_VERSION:
        raise HTTPException(422, 'Versão de protocolo E2EE inválida ou não suportada.')
    iv_b64 = envelope.get('iv')
    ct_b64 = envelope.get('ciphertext')
    if not isinstance(iv_b64, str) or not isinstance(ct_b64, str):
        raise HTTPException(422, 'Campos iv e ciphertext devem ser Base64.')
    try:
        iv_bytes = base64.b64decode(iv_b64.strip(), validate=True)
        ct_bytes = base64.b64decode(ct_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, 'Codificação Base64 inválida no payload E2EE.')
    if len(iv_bytes) != E2EE_GCM_IV_BYTES:
        raise HTTPException(422, 'IV do AES-256-GCM deve possuir exatamente 12 bytes.')
    if len(ct_bytes) <= E2EE_GCM_TAG_BYTES or len(ct_bytes) > E2EE_MAX_CIPHERTEXT_BYTES:
        raise HTTPException(422, 'Ciphertext AES-256-GCM inválido ou sem tag de autenticação de 128 bits.')
    return json.dumps(
        {
            'v': E2EE_CURRENT_VERSION,
            'iv': base64.b64encode(iv_bytes).decode('ascii'),
            'ciphertext': base64.b64encode(ct_bytes).decode('ascii'),
        },
        separators=(',', ':'),
    )


def serialize_binding(row):
    result = dict(row)
    result['id'] = str(result['id'])
    for key in ('created_at', 'expires_at'):
        if result.get(key):
            result[key] = result[key].isoformat()
    result.setdefault('patient_public_key', None)
    result.setdefault('caregiver_public_key', None)
    return result


def serialize_message(row):
    """D47 + SPEC-13: inclui expires_at/deleted_at e o blob cifrado para o Blind Relay."""
    result = dict(row)
    result['id'] = str(result['id'])
    for key in ('sent_at', 'expires_at', 'deleted_at'):
        if result.get(key):
            result[key] = result[key].isoformat()
    if result.get('deleted_at'):
        result['message_text'] = ''
        result['body_encrypted'] = None
        result['encryption_version'] = result.get('encryption_version', 0)
        result['sender_name'] = ''
        result['icon_type'] = 'LOVE'
    else:
        body_enc = result.get('body_encrypted')
        enc_ver = result.get('encryption_version')
        if body_enc:
            result['body_encrypted'] = body_enc
            result['encryption_version'] = enc_ver if enc_ver else E2EE_CURRENT_VERSION
            if not result.get('message_text'):
                result['message_text'] = body_enc
        else:
            result['body_encrypted'] = None
            result['encryption_version'] = enc_ver if enc_ver is not None else 0
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

    def _parse_uuid(value: str):
        """AUD-G14: uuid.UUID() sem guarda transforma input inválido em 500
        (stack trace no log). Devolve 422, que é o correto para input ruim."""
        try:
            return uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, 'ID inválido.')

    @app.post('/api/sync/family-binding')
    async def sync_binding(b: binding_model, request: Request, uid: str = Depends(firebase_uid)):
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_binding_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=pool())
        patient_pub_key = validate_e2ee_public_key(getattr(b, 'patientPublicKey', None))
        async with pool().acquire() as conn, conn.transaction():
            if b.status == 'REVOKED':
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1 FOR UPDATE', _parse_uuid(b.id))
                if not row or uid not in (row['patient_id'], row['caregiver_id']):
                    raise HTTPException(403, 'Vínculo indisponível.')
                await conn.execute("UPDATE family_bindings SET status='REVOKED' WHERE id=$1", row['id'])
                return {'status': 'success', 'id': str(row['id'])}
            require_owner(uid, b.patientId)
            if b.status != 'PENDING' or b.caregiverId:
                raise HTTPException(422, 'Use o convite para conectar o acompanhante.')
            allowed_roles = {'ADMIN_CHILD', 'CAREGIVER_VIEWER', 'PROFESSIONAL_NURSE'}
            allowed_permissions = {'care:read', 'medication:write', 'medication:take', 'medical:share', 'emergency:share'}
            if b.caregiverRole not in allowed_roles or not set(b.permissions).issubset(allowed_permissions):
                raise HTTPException(422, 'Papel ou permissões do cuidador inválidos.')
            code = b.connectionCode.strip().upper()
            if len(code) != 8 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in code):
                raise HTTPException(422, 'Código inválido.')
            if not await conn.fetchval('SELECT self_care_complete FROM profiles WHERE id=$1', uid):
                raise HTTPException(409, 'Conclua e sincronize seu perfil antes de criar um convite.')
            binding_id = _parse_uuid(b.id) if b.id else uuid.uuid4()
            now = datetime.now(timezone.utc)
            # Sanitização Zero-Trust
            caregiver_name = sanitize_zero_trust_text(b.caregiverName, max_length=100, field_name="caregiverName", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
            caregiver_relation = sanitize_zero_trust_text(b.caregiverRelation, max_length=50, field_name="caregiverRelation", ip=client_ip, rate_limiter=rate_limiter, pool=pool())
            row = await conn.fetchrow('''INSERT INTO family_bindings
                (id, patient_id, caregiver_name, caregiver_relation, connection_code, status, created_at, expires_at,
                 caregiver_role, permissions, patient_public_key)
                VALUES ($1,$2,$3,$4,$5,'PENDING',$6,$7,$8,$9,$10)
                ON CONFLICT DO NOTHING RETURNING *''', binding_id, uid, caregiver_name,
                caregiver_relation, code, now, now + timedelta(days=7), b.caregiverRole, b.permissions, patient_pub_key)
            if row is None:
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1', binding_id)
                if not row or row['patient_id'] != uid or row['connection_code'] != code or row['status'] != 'PENDING':
                    raise HTTPException(409, 'Convite indisponível. Gere um novo código.')
                if patient_pub_key and not row.get('patient_public_key'):
                    await conn.execute('UPDATE family_bindings SET patient_public_key=$2 WHERE id=$1', binding_id, patient_pub_key)
            return {'status': 'success', 'id': str(row['id'])}

    @app.get('/api/family/find-by-code/{code}')
    async def find_by_code(code: str, uid: str = Depends(firebase_uid)):
        clean_code = code.strip().upper()
        if clean_code == JOKER_CONNECTION_CODE:
            now = datetime.now(timezone.utc)
            return {
                "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "testfam1-joker")),
                "patient_id": JOKER_PATIENT_UID,
                "patient_name": JOKER_PATIENT_NAME,
                "connection_code": JOKER_CONNECTION_CODE,
                "status": "PENDING",
                "caregiver_name": "",
                "caregiver_relation": "",
                "caregiver_role": "CAREGIVER_VIEWER",
                "permissions": ["ALL"],
                "patient_public_key": None,
                "caregiver_public_key": None,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(days=3650)).isoformat()
            }
        async with pool().acquire() as conn:
            row = await conn.fetchrow('''
                SELECT b.*, p.full_name AS patient_name
                FROM family_bindings b
                LEFT JOIN profiles p ON p.id = b.patient_id
                WHERE b.connection_code = $1 AND b.status = 'PENDING' AND b.expires_at > now()
            ''', clean_code)
            if not row:
                raise HTTPException(404, 'Código indisponível ou expirado.')
            return serialize_binding(row)

    @app.post('/api/family/accept')
    async def accept_invitation(p: AcceptInvitation, request: Request, uid: str = Depends(firebase_uid)):
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_accept_ip", client_ip, limit=10, window_seconds=60, ip=client_ip, pool=pool())
        caregiver_name = sanitize_zero_trust_text(p.caregiverName, max_length=100, field_name="caregiverName", ip=client_ip, rate_limiter=rate_limiter, pool=get_pool())
        caregiver_relation = sanitize_zero_trust_text(p.caregiverRelation, max_length=50, field_name="caregiverRelation", ip=client_ip, rate_limiter=rate_limiter, pool=get_pool())
        if not caregiver_name.strip() or not caregiver_relation.strip():
            raise HTTPException(422, 'Informe nome e relação com o familiar.')
        caregiver_pub_key = validate_e2ee_public_key(p.caregiverPublicKey)

        clean_code = p.code.strip().upper()
        if clean_code == JOKER_CONNECTION_CODE:
            if uid == JOKER_PATIENT_UID:
                raise HTTPException(409, 'Você não pode acompanhar seu próprio perfil.')
            async with pool().acquire() as conn, conn.transaction():
                # Garante que o perfil do paciente modelo de teste exista e esteja completo
                await conn.execute('''
                    INSERT INTO profiles(id, full_name, self_care_complete, role, created_at, updated_at)
                    VALUES ($1, $2, true, 'PATIENT', now(), now())
                    ON CONFLICT (id) DO UPDATE SET self_care_complete=true
                ''', JOKER_PATIENT_UID, JOKER_PATIENT_NAME)

                # Garante stub do perfil do cuidador
                await conn.execute('INSERT INTO profiles(id, full_name) VALUES($1,$2) ON CONFLICT DO NOTHING', uid, caregiver_name)

                # Idempotência: se já tiver vínculo ativo para esse cuidador com o paciente coringa, retorna
                existing = await conn.fetchrow('''
                    SELECT b.*, p.full_name AS patient_name
                    FROM family_bindings b
                    LEFT JOIN profiles p ON p.id = b.patient_id
                    WHERE b.patient_id=$1 AND b.caregiver_id=$2 AND b.status='ACTIVE'
                ''', JOKER_PATIENT_UID, uid)
                if existing:
                    return serialize_binding(existing)

                now = datetime.now(timezone.utc)
                binding_id = uuid.uuid4()
                created = await conn.fetchrow('''
                    INSERT INTO family_bindings (
                        id, patient_id, caregiver_id, caregiver_name, caregiver_relation,
                        connection_code, status, created_at, expires_at, caregiver_role, permissions,
                        caregiver_public_key
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, 'ACTIVE', $7, $8, 'CAREGIVER_VIEWER', '["ALL"]'::jsonb, $9
                    ) RETURNING *
                ''', binding_id, JOKER_PATIENT_UID, uid, caregiver_name, caregiver_relation, JOKER_CONNECTION_CODE, now, now + timedelta(days=3650), caregiver_pub_key)

                res = dict(created)
                res['patient_name'] = JOKER_PATIENT_NAME
                return serialize_binding(res)

        async with pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM family_bindings WHERE connection_code=$1 FOR UPDATE', clean_code)
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
                caregiver_name=$3, caregiver_relation=$4,
                caregiver_public_key=COALESCE($5, caregiver_public_key),
                status='ACTIVE' WHERE id=$1 RETURNING *""",
                row['id'], uid, caregiver_name, caregiver_relation, caregiver_pub_key)
        # doc 10 §4.3: o paciente precisa saber que alguém aceitou o convite.
        # Best-effort, fora da transação: falha de push não desfaz o vínculo.
        if dispatch_push and load_devices and updated is not None:
            patient_uid = updated['patient_id']
            # Plano B (doc 10 §1B): le do banco; o loader injetado e o fallback.
            devices = await load_devices_by_user(get_pool(), load_devices)
            for dev in devices.get(patient_uid) or []:
                token = dev.get('token')
                if not token:
                    continue
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

    @app.post('/api/family/e2ee/public-key')
    async def publish_public_key(p: PublishFamilyPublicKey, uid: str = Depends(firebase_uid)):
        """SPEC-13 §2.1: publica ou atualiza a chave pública ECDH do participante no vínculo familiar."""
        pub_key = validate_e2ee_public_key(p.publicKey)
        if not pub_key:
            raise HTTPException(422, 'Chave pública E2EE obrigatória.')
        async with pool().acquire() as conn, conn.transaction():
            if p.bindingId:
                binding_uuid = _parse_uuid(p.bindingId)
                row = await conn.fetchrow('SELECT * FROM family_bindings WHERE id=$1 FOR UPDATE', binding_uuid)
                if not row or uid not in (row['patient_id'], row.get('caregiver_id')):
                    raise HTTPException(403, 'Vínculo indisponível.')
                if uid == row['patient_id']:
                    updated = await conn.fetchrow(
                        'UPDATE family_bindings SET patient_public_key=$2 WHERE id=$1 RETURNING *',
                        binding_uuid, pub_key)
                else:
                    updated = await conn.fetchrow(
                        'UPDATE family_bindings SET caregiver_public_key=$2 WHERE id=$1 RETURNING *',
                        binding_uuid, pub_key)
                return serialize_binding(updated)
            if not p.patientId:
                raise HTTPException(422, 'Informe bindingId ou patientId.')
            await require_family_access(conn, uid, p.patientId)
            if uid == p.patientId:
                await conn.execute(
                    "UPDATE family_bindings SET patient_public_key=$2 WHERE patient_id=$1 AND status IN ('PENDING','ACTIVE')",
                    p.patientId, pub_key)
            else:
                await conn.execute(
                    "UPDATE family_bindings SET caregiver_public_key=$3 WHERE patient_id=$1 AND caregiver_id=$2 AND status='ACTIVE'",
                    p.patientId, uid, pub_key)
            return {'status': 'success', 'patientId': p.patientId, 'publicKey': pub_key}

    @app.get('/api/family/bindings/{user_id}')
    async def bindings(user_id: str, uid: str = Depends(firebase_uid)):
        require_owner(uid, user_id)
        async with pool().acquire() as conn:
            await conn.execute("UPDATE family_bindings SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=now() AND patient_id=$1", uid)
            rows = await conn.fetch('''SELECT b.*, p.full_name AS patient_name
                FROM family_bindings b LEFT JOIN profiles p ON p.id=b.patient_id
                WHERE b.patient_id=$1 OR b.caregiver_id=$1''', uid)
            return [serialize_binding(row) for row in rows]

    async def _handle_send_family_message(m: message_model, request: Request, uid: str):
        require_owner(uid, m.senderId)
        client_ip = get_client_ip(request)
        if rate_limiter:
            await rate_limiter.check("family_message_ip", client_ip, limit=30, window_seconds=60, ip=client_ip, pool=pool())
        try:
            message_id = uuid.UUID(m.id) if m.id else uuid.uuid4()
        except ValueError:
            raise HTTPException(422, 'Mensagem inválida.')
        sent, expires = message_lifetime(m.sentAt)

        raw_body_encrypted = getattr(m, 'bodyEncrypted', None)
        raw_enc_version = getattr(m, 'encryptionVersion', None)
        if raw_enc_version not in (None, 0, E2EE_CURRENT_VERSION):
            raise HTTPException(422, 'Versão de criptografia E2EE não suportada.')

        if raw_body_encrypted is not None or raw_enc_version == E2EE_CURRENT_VERSION:
            candidate = raw_body_encrypted if raw_body_encrypted is not None else m.messageText
            encrypted_blob = parse_e2ee_envelope(candidate, strict=True)
            encryption_version = E2EE_CURRENT_VERSION
            db_message_text = ''
        else:
            auto_envelope = parse_e2ee_envelope(m.messageText, strict=False)
            if auto_envelope is not None:
                encrypted_blob = auto_envelope
                encryption_version = E2EE_CURRENT_VERSION
                db_message_text = ''
            else:
                clean_text = sanitize_zero_trust_text(
                    m.messageText or '',
                    max_length=500,
                    field_name="messageText",
                    ip=client_ip,
                    rate_limiter=rate_limiter,
                    pool=pool(),
                )
                encrypted_blob = None
                encryption_version = 0
                db_message_text = clean_text

        clean_sender = sanitize_zero_trust_text(m.senderName, max_length=100, field_name="senderName", ip=client_ip, rate_limiter=rate_limiter, pool=pool())

        async with pool().acquire() as conn:
            await require_family_access(conn, uid, m.patientId)
            # D47 + SPEC-13: TTL de 24h por mensagem e persistencia Blind Relay em body_encrypted
            row = await conn.fetchrow('''INSERT INTO family_messages
                (id, patient_id, sender_id, sender_name, message_text, body_encrypted, encryption_version, icon_type, is_read, sent_at, expires_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,false,$9,$10) ON CONFLICT DO NOTHING RETURNING id''',
                message_id, m.patientId, uid, clean_sender, db_message_text, encrypted_blob, encryption_version, m.iconType, sent, expires)
            if row is None and not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM family_messages WHERE id=$1 AND sender_id=$2 AND patient_id=$3)', message_id, uid, m.patientId):
                raise HTTPException(409, 'Mensagem indisponível.')

            # Plano de notificações (doc 10 §1A): só resolve destinatário quando a
            # linha foi efetivamente criada — retentativa idempotente de sync não
            # re-notifica.
            notify_recipients: list[str] = []
            if row is not None:
                # A conversa pertence ao titular, não a um par de usuários.
                # Notifica cada membro ativo uma vez, exceto o autor.
                care_rows = await conn.fetch(
                    "SELECT caregiver_id FROM family_bindings WHERE patient_id=$1 AND status='ACTIVE'",
                    m.patientId)
                members = [m.patientId] + [r['caregiver_id'] for r in care_rows]
                notify_recipients = sorted({member for member in members if member and member != uid})

        push_body = E2EE_PUSH_BODY if encryption_version == E2EE_CURRENT_VERSION else db_message_text[:200]

        # Push fora da transação: falha de FCM nunca pode quebrar o envio da mensagem.
        if row is not None and dispatch_push and load_devices and notify_recipients:
            devices = await load_devices_by_user(get_pool(), load_devices)
            for recipient_uid in notify_recipients:
                for dev in devices.get(recipient_uid) or []:
                    token = dev.get('token')
                    if not token:
                        continue
                    try:
                        await asyncio.to_thread(
                            dispatch_push,
                            token=token,
                            title=f'Mensagem de {clean_sender}',
                            body=push_body,
                            data={
                                'type': 'FAMILY_MESSAGE',
                                'messageId': str(message_id),
                                'senderId': uid,
                                'patientId': m.patientId,
                                'encryptionVersion': str(encryption_version),
                            },
                        )
                    except Exception as push_err:
                        print(f'[FamilyMessage] Falha no push para {recipient_uid}: {push_err}')

        return {'status': 'success', 'id': str(message_id), 'encryptionVersion': encryption_version}

    @app.post('/api/sync/family-message')
    async def message(m: message_model, request: Request, uid: str = Depends(firebase_uid)):
        return await _handle_send_family_message(m, request, uid)

    @app.post('/api/family/messages/send')
    async def send_family_message(m: message_model, request: Request, uid: str = Depends(firebase_uid)):
        return await _handle_send_family_message(m, request, uid)

    async def _handle_get_family_messages(patient_id: str, limit: int, offset: int, uid: str):
        async with pool().acquire() as conn:
            await require_family_access(conn, uid, patient_id)
            # D47: defesa em profundidade — a API nunca devolve mensagem expirada,
            # mesmo que a purga agendada ainda não tenha rodado. Tombstones seguem
            # na resposta para que o outro aparelho aplique a exclusão localmente.
            rows = await conn.fetch('''SELECT * FROM family_messages
                WHERE patient_id=$1 AND expires_at > now()
                ORDER BY sent_at DESC, id DESC LIMIT $2 OFFSET $3''', patient_id, max(1, min(limit, 200)), max(0, offset))
            return [serialize_message(row) for row in rows]

    @app.get('/api/family/messages/{patient_id}')
    async def messages(patient_id: str, limit: int = 50, offset: int = 0, uid: str = Depends(firebase_uid)):
        return await _handle_get_family_messages(patient_id, limit, offset, uid)

    @app.get('/api/family/messages')
    async def messages_query(patient_id: str, limit: int = 50, offset: int = 0, uid: str = Depends(firebase_uid)):
        return await _handle_get_family_messages(patient_id, limit, offset, uid)

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
                    (id,patient_id,sender_id,sender_name,message_text,body_encrypted,encryption_version,icon_type,is_read,sent_at,expires_at,deleted_at)
                    VALUES($1,$2,$3,'','',NULL,0,'LOVE',false,$4,$5,now()) ON CONFLICT DO NOTHING""",
                    message_id, p.patientId, uid, sent, expires)
                row = await conn.fetchrow('SELECT id, patient_id, sender_id FROM family_messages WHERE id=$1 FOR UPDATE', message_id)
            if row['sender_id'] != uid:
                raise HTTPException(403, 'Só quem enviou pode apagar a mensagem.')
            await conn.execute("UPDATE family_messages SET deleted_at=COALESCE(deleted_at,now()), message_text='', body_encrypted=NULL, sender_name='', icon_type='LOVE' WHERE id=$1", message_id)
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
        devices = await load_devices_by_user(get_pool(), load_devices)
        for recipient_uid in recipients:
            for dev in devices.get(recipient_uid) or []:
                token = dev.get('token')
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
        return {'id': str(row['id']), 'title': row['title'], 'scheduledDate': row['scheduled_date'].isoformat(), 'status': row['status']}

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
        return {'id': str(updated['id']), 'status': updated['status']}

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
        return {'id': str(updated['id']), 'status': updated['status']}

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
        return {'id': str(updated['id']), 'status': updated['status']}

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
        return {'id': str(updated['id']), 'status': updated['status']}

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
