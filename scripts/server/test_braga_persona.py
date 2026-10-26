import unittest
from braga_persona import PERSONA
from orb_llm import build_speak_payload
from orb_router import route_chat


class ConversationContractTests(unittest.TestCase):
    def test_shared_persona_reaches_model_with_history(self):
        history = [{"role": "user", "content": "Hoje não estou para brincadeira"}]
        payload = build_speak_payload("test", history, 0.1, 220)
        self.assertTrue(payload["messages"][0]["content"].startswith(PERSONA))
        self.assertEqual(payload["messages"][1:], history)

    def test_food_conversation_is_not_forced_to_registration(self):
        for utterance in ("almocei arroz", "o que posso comer hoje", "você gosta de maçã"):
            with self.subTest(utterance=utterance):
                reply = route_chat(utterance)
                if reply is not None:
                    self.assertEqual(reply["acao"], "CONVERSA")
                    self.assertIn("descontinuado", reply["fala"])

    def test_explicit_meal_request_explains_actual_capability(self):
        reply = route_chat("registre minha refeição")
        self.assertEqual(reply["acao"], "CONVERSA")
        self.assertIn("descontinuado", reply["fala"])
        self.assertNotIn("anota item por item", reply["fala"])


if __name__ == '__main__':
    unittest.main()
