"""Testes unitários e de contrato do Blind Relay E2EE no Chat Familiar (SPEC-13)."""
import base64
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from api_identity import firebase_uid
from family_api import (
    E2EE_CURRENT_VERSION,
    E2EE_PUSH_BODY,
    install_family_routes,
    parse_e2ee_envelope,
    serialize_binding,
    serialize_message,
    validate_e2ee_public_key,
)
from test_alignment import namespace


def _sample_e2ee_envelope(iv_bytes: int = 12, ct_bytes: int = 48, version: int = 1) -> str:
    iv = base64.b64encode(os.urandom(iv_bytes)).decode('ascii')
    ct = base64.b64encode(os.urandom(ct_bytes)).decode('ascii')
    return json.dumps({'v': version, 'iv': iv, 'ciphertext': ct})


def _sample_public_key_b64(size: int = 91) -> str:
    return base64.b64encode(os.urandom(size)).decode('ascii')


class FamilyE2eeValidationTest(unittest.TestCase):
    """Valida a integridade estrutural de chaves públicas e envelopes AES-256-GCM."""

    def test_valid_e2ee_envelope_is_canonicalized(self):
        raw = _sample_e2ee_envelope()
        parsed = parse_e2ee_envelope(raw, strict=True)
        self.assertIsNotNone(parsed)
        decoded = json.loads(parsed)
        self.assertEqual(E2EE_CURRENT_VERSION, decoded['v'])
        self.assertEqual(12, len(base64.b64decode(decoded['iv'])))
        self.assertGreater(len(base64.b64decode(decoded['ciphertext'])), 16)

    def test_rejects_invalid_iv_length_or_missing_gcm_tag(self):
        for bad_envelope in (
            _sample_e2ee_envelope(iv_bytes=8, ct_bytes=32),
            _sample_e2ee_envelope(iv_bytes=16, ct_bytes=32),
            _sample_e2ee_envelope(iv_bytes=12, ct_bytes=16),  # Sem pelo menos 1 byte de payload + 16 de tag
            _sample_e2ee_envelope(iv_bytes=12, ct_bytes=32, version=2),
            '{"v":1,"iv":"@@@","ciphertext":"@@@"}',
            'texto-plano-invalido',
        ):
            with self.subTest(bad_envelope=bad_envelope):
                with self.assertRaises(HTTPException) as ctx:
                    parse_e2ee_envelope(bad_envelope, strict=True)
                self.assertEqual(422, ctx.exception.status_code)

    def test_validates_ecdh_public_key_bounds(self):
        valid_key = _sample_public_key_b64(91)
        self.assertEqual(valid_key, validate_e2ee_public_key(valid_key))
        self.assertIsNone(validate_e2ee_public_key(None))
        self.assertIsNone(validate_e2ee_public_key('   '))

        for bad_key in ('nao-base64!!!', base64.b64encode(b'curta').decode('ascii')):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(HTTPException) as ctx:
                    validate_e2ee_public_key(bad_key)
                self.assertEqual(422, ctx.exception.status_code)


class FamilyE2eeBlindRelayRoutesTest(unittest.TestCase):
    """Garante que o gateway atua como Blind Relay sem armazenar texto plano no RDS."""

    def setUp(self):
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.conn.fetchrow.return_value = {'id': uuid.UUID(int=10)}
        self.conn.fetch.return_value = [{'caregiver_id': 'caregiver'}]
        self.conn.fetchval.return_value = True

        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        self.dispatch = MagicMock(return_value=True)
        self.devices = {
            'patient': {'token': 'patient-token'},
            'caregiver': {'token': 'caregiver-token'},
        }

        self.app = FastAPI()
        install_family_routes(
            self.app,
            lambda: self.pool,
            namespace['FamilyBindingSyncDto'],
            namespace['FamilyMessageSyncDto'],
            load_devices=lambda: self.devices,
            dispatch_push=self.dispatch,
        )
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        self.client = TestClient(self.app)
        self.msg_id = str(uuid.uuid4())
        self.now_iso = datetime.now(timezone.utc).isoformat()

    def test_send_encrypted_message_persists_only_ciphertext_and_masks_push(self):
        envelope = _sample_e2ee_envelope()
        payload = {
            'id': self.msg_id,
            'patientId': 'patient',
            'senderId': 'caregiver',
            'senderName': 'Maria',
            'messageText': '',
            'bodyEncrypted': envelope,
            'encryptionVersion': 1,
            'iconType': 'LOVE',
            'isRead': False,
            'sentAt': self.now_iso,
        }
        res = self.client.post('/api/family/messages/send', json=payload)
        self.assertEqual(200, res.status_code)
        self.assertEqual(1, res.json()['encryptionVersion'])

        sql, *params = self.conn.fetchrow.call_args.args
        self.assertIn('body_encrypted', sql)
        self.assertIn('encryption_version', sql)
        # message_text ($5) fica vazio no banco; apenas body_encrypted ($6) recebe o blob cifrado
        db_message_text = params[4]
        db_body_encrypted = params[5]
        db_enc_version = params[6]
        self.assertEqual('', db_message_text)
        self.assertEqual(parse_e2ee_envelope(envelope, strict=True), db_body_encrypted)
        self.assertEqual(1, db_enc_version)

        # Push FCM nunca vaza o blob cifrado nem exige texto plano
        self.dispatch.assert_called_once()
        self.assertEqual(E2EE_PUSH_BODY, self.dispatch.call_args.kwargs['body'])
        self.assertEqual('1', self.dispatch.call_args.kwargs['data']['encryptionVersion'])

    def test_sync_family_message_auto_detects_e2ee_envelope_in_message_text(self):
        envelope = _sample_e2ee_envelope()
        payload = {
            'id': self.msg_id,
            'patientId': 'patient',
            'senderId': 'caregiver',
            'senderName': 'Maria',
            'messageText': envelope,
            'iconType': 'MED',
            'isRead': False,
            'sentAt': self.now_iso,
        }
        res = self.client.post('/api/sync/family-message', json=payload)
        self.assertEqual(200, res.status_code)
        _, *params = self.conn.fetchrow.call_args.args
        self.assertEqual('', params[4])
        self.assertEqual(parse_e2ee_envelope(envelope, strict=True), params[5])
        self.assertEqual(1, params[6])

    def test_get_messages_returns_encrypted_blob_and_preserves_legacy_messages(self):
        envelope = parse_e2ee_envelope(_sample_e2ee_envelope(), strict=True)
        now = datetime.now(timezone.utc)
        self.conn.fetch.return_value = [
            {
                'id': uuid.uuid4(),
                'patient_id': 'patient',
                'sender_id': 'caregiver',
                'sender_name': 'Maria',
                'message_text': '',
                'body_encrypted': envelope,
                'encryption_version': 1,
                'icon_type': 'LOVE',
                'is_read': False,
                'sent_at': now,
                'expires_at': now + timedelta(hours=24),
                'deleted_at': None,
            },
            {
                'id': uuid.uuid4(),
                'patient_id': 'patient',
                'sender_id': 'patient',
                'sender_name': 'Carlos',
                'message_text': 'Mensagem antiga antes do E2EE',
                'body_encrypted': None,
                'encryption_version': 0,
                'icon_type': 'LOVE',
                'is_read': True,
                'sent_at': now - timedelta(minutes=10),
                'expires_at': now + timedelta(hours=23),
                'deleted_at': None,
            },
        ]

        res = self.client.get('/api/family/messages?patient_id=patient')
        self.assertEqual(200, res.status_code)
        items = res.json()
        self.assertEqual(2, len(items))

        e2ee_item = items[0]
        self.assertEqual(1, e2ee_item['encryption_version'])
        self.assertEqual(envelope, e2ee_item['body_encrypted'])
        self.assertEqual(envelope, e2ee_item['message_text'])

        legacy_item = items[1]
        self.assertEqual(0, legacy_item['encryption_version'])
        self.assertIsNone(legacy_item['body_encrypted'])
        self.assertEqual('Mensagem antiga antes do E2EE', legacy_item['message_text'])

    def test_tombstone_scrubs_encrypted_body(self):
        now = datetime.now(timezone.utc)
        serialized = serialize_message({
            'id': uuid.uuid4(),
            'message_text': '',
            'body_encrypted': _sample_e2ee_envelope(),
            'encryption_version': 1,
            'sender_name': 'Maria',
            'icon_type': 'MED',
            'deleted_at': now,
        })
        self.assertEqual('', serialized['message_text'])
        self.assertIsNone(serialized['body_encrypted'])
        self.assertEqual('', serialized['sender_name'])

    def test_public_key_exchange_on_binding_create_accept_and_publish(self):
        patient_pub = _sample_public_key_b64(91)
        caregiver_pub = _sample_public_key_b64(91)
        binding_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        # 1. Paciente cria convite já publicando sua chave pública ECDH
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        self.conn.fetchrow.return_value = {
            'id': binding_id,
            'patient_id': 'patient',
            'caregiver_id': None,
            'caregiver_name': 'Maria',
            'caregiver_relation': 'Filha',
            'connection_code': 'ABCD1234',
            'status': 'PENDING',
            'patient_public_key': patient_pub,
            'caregiver_public_key': None,
            'created_at': now,
            'expires_at': now + timedelta(days=7),
        }
        create_res = self.client.post(
            '/api/sync/family-binding',
            json={
                'id': str(binding_id),
                'patientId': 'patient',
                'caregiverName': 'Maria',
                'caregiverRelation': 'Filha',
                'connectionCode': 'ABCD1234',
                'status': 'PENDING',
                'patientPublicKey': patient_pub,
                'createdAt': now.isoformat(),
                'expiresAt': (now + timedelta(days=7)).isoformat(),
            },
        )
        self.assertEqual(200, create_res.status_code)
        insert_sql, *insert_args = self.conn.fetchrow.call_args.args
        self.assertIn('patient_public_key', insert_sql)
        self.assertIn(patient_pub, insert_args)

        # 2. Cuidador aceita convite enviando sua chave pública ECDH
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        pending_row = {
            'id': binding_id,
            'patient_id': 'patient',
            'caregiver_id': None,
            'caregiver_name': 'Maria',
            'caregiver_relation': 'Filha',
            'connection_code': 'ABCD1234',
            'status': 'PENDING',
            'patient_public_key': patient_pub,
            'caregiver_public_key': None,
            'created_at': now,
            'expires_at': now + timedelta(days=7),
        }
        active_row = dict(
            pending_row,
            caregiver_id='caregiver',
            status='ACTIVE',
            caregiver_public_key=caregiver_pub,
        )
        self.conn.fetchrow.side_effect = [pending_row, active_row]
        accept_res = self.client.post(
            '/api/family/accept',
            json={
                'code': 'ABCD1234',
                'caregiverName': 'Maria',
                'caregiverRelation': 'Filha',
                'caregiverPublicKey': caregiver_pub,
            },
        )
        self.assertEqual(200, accept_res.status_code)
        body = accept_res.json()
        self.assertEqual(patient_pub, body['patient_public_key'])
        self.assertEqual(caregiver_pub, body['caregiver_public_key'])

        # 3. Rotação/publicação explícita de chave pública via endpoint dedicado
        rotated_pub = _sample_public_key_b64(91)
        self.conn.fetchrow.side_effect = [
            active_row,
            dict(active_row, caregiver_public_key=rotated_pub),
        ]
        pub_res = self.client.post(
            '/api/family/e2ee/public-key',
            json={'bindingId': str(binding_id), 'publicKey': rotated_pub},
        )
        self.assertEqual(200, pub_res.status_code)
        self.assertEqual(rotated_pub, pub_res.json()['caregiver_public_key'])
        self.assertIsNotNone(serialize_binding(active_row)['patient_public_key'])


if __name__ == '__main__':
    unittest.main()
