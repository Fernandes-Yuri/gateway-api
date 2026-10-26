"""Testes do contrato do agente B0."""
import unittest

from orb_actions import (
    ACTION_CATALOG,
    actions_for_channel,
    closing_fala,
)


class ContractTests(unittest.TestCase):
    def test_catalog_shape(self):
        for action, spec in ACTION_CATALOG.items():
            with self.subTest(action=action):
                self.assertTrue(spec["channels"])
                self.assertIn("closing", spec)
                self.assertIn("needs_confirmation", spec)

    def test_channel_split(self):
        chat = actions_for_channel("chat")
        voice = actions_for_channel("voice")
        self.assertEqual(actions_for_channel("whatsapp"), ())
        self.assertNotIn("ABRIR_LISTA_COMPRAS", chat)
        self.assertNotIn("REGISTRAR_PRESSAO", chat)
        self.assertIn("REGISTRAR_PRESSAO", voice)
        self.assertNotIn("ABRIR_LISTA_COMPRAS", voice)
        self.assertIn("EMERGENCIA", chat)
        self.assertIn("EMERGENCIA", voice)

    def test_closing_never_lies(self):
        for action in ACTION_CATALOG:
            fala = closing_fala(action, {"items": ["aveia"], "sistolica": 120,
                                         "diastolica": 80, "glicemia": 100,
                                         "quantidade_ml": 250})
            for banned in ("Já registrei", "Já salvei", "Já abri", "Já anotei", "registrado com"):
                self.assertNotIn(banned, fala)
        self.assertEqual(closing_fala("NOPE", {}), "Pronto! Confirma na tela?")


if __name__ == "__main__":
    unittest.main()
