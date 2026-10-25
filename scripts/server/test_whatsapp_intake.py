"""Contrato, validação de entidades e prontidão; nenhum provedor real."""
import unittest
from pathlib import Path

from pydantic import ValidationError

from schemas.whatsapp_intake import ModelReply, ControlRequest
from services.wa_intake_worker import merge_fields, pending_fields, summary_text
from services.wa_intake_transport import TransportFailure, validate_media


class EntityTests(unittest.TestCase):
    def result(self, updates):
        return ModelReply(reply='Me conta um pouco.', updates=updates)

    def test_evidence_and_fields_cannot_be_invented(self):
        result = self.result({'patient.age_years': {'value': 68, 'status': 'provided', 'evidence': '68 anos'}})
        with self.assertRaises(ValueError):
            merge_fields({}, result, 'É para minha mãe', 1)
        for updates in ({'operator_uid': {'value': 'attacker', 'status': 'provided', 'evidence': 'a'}},
                        {'patient.age_years': {'value': 150, 'status': 'provided', 'evidence': '150'}},
                        {'care_for': {'value': 'mother', 'status': 'provided', 'evidence': 'mãe'}}):
            with self.assertRaises(ValidationError): self.result(updates)

    def test_refusal_is_answer_and_gender_is_never_derived_from_name(self):
        result = self.result({'contact.full_name': {'value': 'Alex Souza', 'status': 'provided', 'evidence': 'Alex Souza'},
                              'contact.gender': {'value': None, 'status': 'declined', 'evidence': 'prefiro não dizer'}})
        fields = merge_fields({}, result, 'Alex Souza, prefiro não dizer', 2)
        self.assertEqual(fields['contact.gender']['status'], 'declined')
        self.assertNotIn('contact.gender', pending_fields(fields))
        self.assertNotIn('patient.full_name', fields)
        self.assertIn('Prefere não informar', summary_text(fields))

    def test_self_to_other_clears_derived_patient_identity(self):
        original = {'contact.full_name': {'value': 'Alex Souza', 'status': 'provided'},
                    'care_for': {'value': 'self', 'status': 'provided'}}
        fields = merge_fields(original, self.result({}), '', 1)
        self.assertEqual(fields['patient.full_name']['value'], 'Alex Souza')
        fields = merge_fields(fields, self.result({'care_for': {'value': 'other', 'status': 'provided', 'evidence': 'minha mãe'}}), 'minha mãe', 2)
        self.assertNotIn('patient.full_name', fields)
        self.assertIn('patient.full_name', pending_fields(fields))

    def test_control_requires_bounded_idempotency_key(self):
        with self.assertRaises(ValidationError): ControlRequest(epoch=-1, idempotency_key='abcdefgh')
        with self.assertRaises(ValidationError): ControlRequest(epoch=0, idempotency_key='../unexpected')

    def test_runtime_prompt_matches_reviewed_persona(self):
        root = Path(__file__).resolve().parents[2]
        runtime = (Path(__file__).parent / 'prompts/whatsapp_intake_v1.txt').read_text()
        self.assertEqual(runtime, (root / 'docs/prompts/whatsapp-clinic-intake-v1.txt').read_text())
        self.assertIn('assistente virtual', runtime)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_and_oversized_image_are_rejected(self):
        for content in (b'<html>bad</html>', b'\xff\xd8\xff' + b'x' * (6 * 1024 * 1024)):
            with self.assertRaises(TransportFailure): await validate_media(content, 'image/jpeg', 'image')
        with self.assertRaises(TransportFailure): await validate_media(b'not audio', 'text/html', 'audio')


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_revalidates_operator_and_closes_after_revocation(self):
        from unittest.mock import AsyncMock,MagicMock,patch
        from fastapi import HTTPException
        from routers import whatsapp_internal as api
        socket=MagicMock()
        socket.headers={'authorization':'Bearer wha_synthetic'}
        socket.query_params={}
        for name in ('accept','close','send_text'): setattr(socket,name,AsyncMock())
        with patch.object(api,'pool',return_value=MagicMock()), \
             patch.object(api.sessions,'authenticate',AsyncMock(side_effect=['yuri','yuri',HTTPException(401,'revoked')])), \
             patch.object(api,'event_page',AsyncMock(return_value={'items':[],'next_cursor':0,'has_more':False})), \
             patch.object(api.asyncio,'sleep',AsyncMock()):
            await api.stream(socket)
        socket.accept.assert_awaited_once()
        socket.close.assert_awaited_once_with(code=1008,reason='authorization_required')
