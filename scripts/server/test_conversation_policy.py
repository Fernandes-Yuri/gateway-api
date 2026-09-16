import unittest
from braga_persona import PERSONA
from conversation_policy import APP_SYSTEM, build_conversation_system
from orb_llm import SPEAK_SYSTEM, build_speak_payload


class PolicyTests(unittest.TestCase):
    def test_channels_share_canonical_persona_and_clock(self):
        for channel in ('voice', 'chat', 'whatsapp'):
            prompt = build_conversation_system(channel)
            self.assertTrue(prompt.startswith(PERSONA))
            self.assertIn('Relógio de referência', prompt)

    def test_whatsapp_does_not_promise_app_navigation(self):
        prompt = build_conversation_system('whatsapp')
        self.assertIn('Nunca diga que abriu uma tela', prompt)
        self.assertIn('mensagens ou botões', prompt)

    def test_app_policy_and_preferences_preserved(self):
        self.assertEqual(SPEAK_SYSTEM, APP_SYSTEM)
        payload = build_speak_payload('m', [], .1, 100, {'cup_ml': 300})
        self.assertIn('"cup_ml": 300', payload['messages'][0]['content'])

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError): build_conversation_system('unknown')


if __name__ == '__main__': unittest.main()
