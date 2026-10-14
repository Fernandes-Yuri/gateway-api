"""Acesso inicial e continuidade, sem chamadas reais ao modelo/WhatsApp."""
import unittest
from unittest.mock import AsyncMock, patch

from services import wa_session_context as context
from services.wa_onboarding import DOWNLOAD_URL, unlinked_reply, preference_reply, clear
from wa_gate_service import gate_unregistered

PHONE = '5500000000000'


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear(PHONE)

    async def test_download_linking_and_help_answer_the_actual_question(self):
        for question, expected in (('como baixar o app', 'Android'),
                                   ('como vincular meu app', 'código'),
                                   ('Como você pode me ajudar', 'registros')):
            send = AsyncMock()
            self.assertTrue(await gate_unregistered(question, {'status': 'unlinked'}, send, PHONE))
            self.assertIn(expected, send.await_args.args[1])
            self.assertIn(DOWNLOAD_URL, send.await_args.args[1])
        responses = [unlinked_reply('quero baixar o aplicativo', PHONE) for _ in range(3)]
        self.assertEqual(len(set(responses)), 3)

    async def test_unavailable_and_ambiguous_never_claim_unlinked(self):
        for status in ('unavailable', 'ambiguous'):
            send = AsyncMock()
            await gate_unregistered('como baixar', {'status': status}, send, PHONE)
            self.assertNotIn(DOWNLOAD_URL, send.await_args.args[1])

    async def test_preference_questions_are_distinct_without_promising_storage(self):
        nickname = preference_reply('Pode me chamar de Léo', PHONE)
        style = preference_reply('Prefiro respostas curtas', PHONE)
        memory = preference_reply('O que você lembra de mim?', PHONE)
        self.assertEqual(len({nickname, style, memory}), 3)
        self.assertIn('não salvo apelidos', nickname)
        self.assertIn('Não salvo', style)
        self.assertIn('registros confirmados', memory)
        self.assertNotEqual(nickname, preference_reply('Pode me chamar de Léo', PHONE))


class ContextTests(unittest.TestCase):
    def setUp(self):
        context._sessions.clear()
        self.token = context.scope(('phone-hash', 'account-a'))
        self.addCleanup(context.reset, self.token)

    def test_order_and_no_duplicate_current_message(self):
        context.record('user', 'vomitei duas vezes')
        context.record('assistant', 'Há quanto tempo começou?')
        context.record('user', 'desde ontem')
        self.assertEqual(context.messages('policy', 'desde ontem'), [
            {'role': 'system', 'content': 'policy'},
            {'role': 'user', 'content': 'vomitei duas vezes'},
            {'role': 'assistant', 'content': 'Há quanto tempo começou?'},
            {'role': 'user', 'content': 'desde ontem'}])

    def test_isolation_expiry_and_deletion(self):
        context.record('user', 'private-a')
        context.scope(('other-phone', 'account-b'))
        self.assertNotIn('private-a', str(context.messages('policy', 'oi')))
        context.scope(('phone-hash', 'account-b'))
        self.assertNotIn('private-a', str(context.messages('policy', 'oi')))
        context.scope(('phone-hash', 'account-a'))
        with patch('services.wa_session_context.monotonic', return_value=10**12):
            self.assertNotIn('private-a', str(context.messages('policy', 'oi')))
        context.record('user', 'delete-me')
        context.clear('phone-hash')
        self.assertNotIn('delete-me', str(context.messages('policy', 'oi')))

    def test_bounds_and_no_system_role_from_dialogue(self):
        for _ in range(30):
            context.record('user', 'x' * 3000)
        context.record('system', 'untrusted instructions')
        messages = context.messages('policy', 'current')
        self.assertLessEqual(len(messages), context.MAX_MESSAGES + 2)
        self.assertLessEqual(sum(len(m['content']) for m in messages[1:]), context.MAX_CHARS + 7)
        self.assertEqual(sum(m['role'] == 'system' for m in messages), 1)
