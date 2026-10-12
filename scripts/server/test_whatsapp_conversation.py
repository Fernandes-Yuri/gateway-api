import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from whatsapp_conversation import (
    FALLBACK_REPLY, identity_reply, parse_conversation_reply, public_reply,
)


class PublicReplyTests(unittest.TestCase):
    def test_screenshot_response_removes_internal_text_and_empty_json(self):
        content = '<think>Internal text with JSON {}.</think> Olá, como posso ajudar?'
        self.assertEqual(parse_conversation_reply(content),
                         ('Olá, como posso ajudar?', 'CONVERSA', {}))

    def test_multiple_and_nested_blocks_are_removed(self):
        self.assertEqual(public_reply('<THINK>a<think>b</think>c</THINK>Olá<think>d</think>!'), 'Olá!')

    def test_truncated_reasoning_does_not_become_a_response(self):
        for content in ('<think>unfinished', '<think>one</think><think>unfinished', None):
            self.assertEqual(parse_conversation_reply(content), (FALLBACK_REPLY, 'CONVERSA', {}))

    def test_orphan_closing_tag_hides_the_prefix(self):
        self.assertEqual(public_reply('internal prefix</think>Resposta.'), 'Resposta.')

    def test_actions_inside_reasoning_are_not_executed(self):
        content = '<think>{"fala":"salvo", "acao":"REGISTRAR_AGUA", "parametros":{"ml":500}}</think>Olá.'
        self.assertEqual(parse_conversation_reply(content), ('Olá.', 'CONVERSA', {}))

    def test_envelope_preserves_public_action_and_removes_nested_reasoning(self):
        envelope = {'fala': '<think>internal</think>Confirme o valor.',
                    'acao': 'REGISTRAR_AGUA', 'parametros': {'ml': 200}}
        self.assertEqual(parse_conversation_reply('```json\n' + json.dumps(envelope) + '\n```'),
                         ('Confirme o valor.', 'REGISTRAR_AGUA', {'ml': 200}))

    def test_empty_or_non_object_envelope_does_not_expose_internal_content(self):
        self.assertEqual(parse_conversation_reply('{"fala":"<think>unfinished"}'),
                         (FALLBACK_REPLY, 'CONVERSA', {}))
        self.assertEqual(parse_conversation_reply('[]'), ('[]', 'CONVERSA', {}))


class IdentityTests(unittest.TestCase):
    def test_verified_account_uses_registered_name(self):
        for question in ('Quem sou eu?', 'Qual é meu nome?', 'Você sabe meu nome?'):
            reply = identity_reply(question, verified_link=True, full_name='Ana Silva')
            self.assertIn('Ana Silva', reply)
            self.assertIn('vinculado', reply)

    def test_legacy_phone_or_emergency_contact_does_not_reveal_profile_name(self):
        reply = identity_reply('Quem sou eu?', verified_link=False, full_name='Outra Pessoa')
        self.assertNotIn('Outra Pessoa', reply)
        self.assertIn('vincule', reply)

    def test_missing_name_does_not_invent_identity(self):
        reply = identity_reply('Quem sou eu?', verified_link=True, full_name=None)
        self.assertIn('não encontrei seu nome', reply)

    def test_other_questions_still_use_the_existing_conversation_flow(self):
        self.assertIsNone(identity_reply('Quem sou eu para julgar?', verified_link=True, full_name='Ana'))


class WhatsAppPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from routers import whatsapp
        self.wa = whatsapp
        self.send = AsyncMock()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={'id': 'test-account', 'full_name': 'Ana Silva',
                                               'user_role': 'PATIENT'})
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        self.client = MagicMock()
        self.client.post = AsyncMock()
        http_client = MagicMock()
        http_client.return_value.__aenter__.return_value = self.client
        import asyncio
        self.patches = [
            patch.object(whatsapp._state, 'db_pool', pool),
            patch.object(whatsapp._state, 'sem_wa', asyncio.Semaphore(1)),
            patch.multiple(whatsapp, HAS_WHATSAPP=True, HAS_WA_LINK=False, HAS_WA_GATE=False),
            patch.object(whatsapp, 'send_whatsapp_text', self.send),
            patch.object(whatsapp, 'log_fine_tuning_sample'),
            patch.object(whatsapp, 'trace_llm'),
            patch.object(whatsapp, 'is_prompt_injection', return_value=False),
            patch.object(whatsapp, 'is_warm_and_affectionate', return_value=False),
            patch.object(whatsapp, 'match_fast_lane_intent', return_value=None),
            patch.object(whatsapp, 'resolve_followup', return_value=None),
            patch.object(whatsapp, 'detect_all', return_value={'kind': 'conversation'}),
            patch.object(whatsapp, 'whatsapp_next', return_value={'type': 'llm'}),
            patch.object(whatsapp.httpx, 'AsyncClient', http_client),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def deliver(self, text):
        await self.wa.process_incoming_whatsapp_payload({'entry': [{'changes': [{'value': {
            'messages': [{'from': '5500000000000', 'type': 'text', 'text': {'body': text}}]
        }}]}]})

    async def test_identity_after_link_uses_database_without_calling_model(self):
        await self.deliver('Quem sou eu?')
        self.send.assert_awaited_once_with(
            '5500000000000', 'Você é Ana Silva. Seu WhatsApp está vinculado à sua conta do Braga Saúde.')
        self.client.post.assert_not_awaited()

    async def test_model_reasoning_never_reaches_the_text_sender(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {'choices': [{'message': {
            'content': '<think>Internal reasoning with JSON {}.</think>Resposta pública.'}}]}
        self.client.post.return_value = response
        await self.deliver('Conte algo')
        self.send.assert_awaited_once_with('5500000000000', 'Resposta pública.')

    async def test_audio_request_gets_only_text_without_model_or_media_upload(self):
        with patch.object(self.wa, 'match_fast_lane_intent', return_value='ask_audio'):
            await self.deliver('Manda áudio')
        self.send.assert_awaited_once_with(
            '5500000000000', 'Por enquanto, minhas respostas no WhatsApp são somente por texto. Como posso te ajudar hoje?')
        self.client.post.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
