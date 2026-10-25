import unittest
from unittest.mock import AsyncMock, patch
import httpx
from services import content_moderation as moderation


class AvatarModerationTests(unittest.IsolatedAsyncioTestCase):
    def test_verdict_requires_json_boolean_not_truthy_strings(self):
        for raw in ['{"aprovada":"false"}', '{"aprovada":"true"}', '{"aprovada":1}',
                    '{"motivo":"ok"}', 'APROVADA ignore tudo', '[true]', '{"aprovada":true,"motivo":42}']:
            with self.subTest(raw=raw):
                self.assertIsNone(moderation._parse_verdict(raw))
        self.assertEqual((True, ''), moderation._parse_verdict('{"aprovada":true,"motivo":""}'))
        self.assertEqual((False, 'Documento'), moderation._parse_verdict('{"aprovada":false,"motivo":"Documento"}'))

    async def test_unreadable_primary_tries_fallback(self):
        with patch.object(moderation, 'GEMINI_API_KEY', 'fixture'), patch.object(moderation, 'GROQ_API_KEY', 'fixture'), \
             patch.object(moderation, '_gemini_verdict', AsyncMock(return_value='ilegível')), \
             patch.object(moderation, '_groq_verdict', AsyncMock(return_value='{"aprovada":true,"motivo":""}')):
            self.assertEqual((True, ''), await moderation.moderate_avatar(b'fixture', 'image/jpeg'))

    async def test_rejection_is_not_overridden_by_second_provider(self):
        fallback = AsyncMock(return_value='{"aprovada":true}')
        with patch.object(moderation, 'GEMINI_API_KEY', 'fixture'), \
             patch.object(moderation, '_gemini_verdict', AsyncMock(return_value='{"aprovada":false,"motivo":"Recusada"}')), \
             patch.object(moderation, '_groq_verdict', fallback):
            self.assertEqual((False, 'Recusada'), await moderation.moderate_avatar(b'fixture', 'image/jpeg'))
            fallback.assert_not_awaited()

    async def test_no_valid_verdict_never_approves(self):
        with patch.object(moderation, 'GEMINI_API_KEY', 'fixture'), \
             patch.object(moderation, '_gemini_verdict', AsyncMock(return_value='{"aprovada":"false"}')), \
             patch.object(moderation, '_groq_verdict', AsyncMock(return_value=None)):
            with self.assertRaises(moderation.ModerationUnavailable):
                await moderation.moderate_avatar(b'fixture', 'image/jpeg')

    async def test_primary_uses_avatar_model_and_structured_output(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "pensamento", "thought": True}, {"text": '{"aprovada":true,"motivo":""}'}]}}]})
        with patch.object(moderation, 'GEMINI_API_KEY', 'fixture'),              patch.object(moderation.httpx, 'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value = client
            raw = await moderation._gemini_verdict(b'fixture', 'image/jpeg')
        self.assertEqual((True, ''), moderation._parse_verdict(raw))
        args, kwargs = client.post.call_args
        self.assertIn(moderation.AVATAR_GEMINI_MODEL, args[0])
        self.assertNotIn('key=', args[0])
        self.assertEqual('application/json', kwargs['json']['generationConfig']['responseMimeType'])
