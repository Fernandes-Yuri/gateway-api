import unittest
from braga_persona import PERSONA
from conversation_policy import APP_SYSTEM, build_conversation_system
from orb_llm import SPEAK_SYSTEM, build_speak_payload


class PolicyTests(unittest.TestCase):
    def test_channels_share_canonical_persona_and_clock(self):
        for channel in ('voice', 'chat'):
            prompt = build_conversation_system(channel)
            self.assertTrue(prompt.startswith(PERSONA))
            self.assertIn('Relógio de referência', prompt)


    def test_never_invents_medication_schedule_or_denies_visible_history(self):
        for channel in ('chat',):
            prompt = build_conversation_system(channel)
            self.assertIn('Nunca invente ou sugira medicamento, dose ou horário', prompt)
            self.assertIn('reconheça o que você escreveu', prompt)

    def test_app_policy_and_preferences_preserved(self):
        self.assertEqual(SPEAK_SYSTEM, APP_SYSTEM)
        payload = build_speak_payload('m', [], .1, 100, {'cup_ml': 300})
        self.assertIn('"cup_ml": 300', payload['messages'][0]['content'])

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError): build_conversation_system('unknown')


if __name__ == '__main__': unittest.main()
