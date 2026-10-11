"""Testes do contrato do agente B0."""
import unittest

from orb_actions import (
    ACTION_CATALOG,
    actions_for_channel,
    closing_fala,
    extract_grocery_items,
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
        wa = actions_for_channel("whatsapp")
        self.assertIn("ABRIR_LISTA_COMPRAS", chat)
        self.assertNotIn("REGISTRAR_PRESSAO", chat)
        self.assertIn("REGISTRAR_PRESSAO", wa)
        self.assertNotIn("ABRIR_LISTA_COMPRAS", wa)
        self.assertIn("EMERGENCIA", chat)
        self.assertIn("EMERGENCIA", wa)

    def test_grocery_slots(self):
        self.assertEqual(extract_grocery_items("adiciona aveia e banana na lista"),
                         ["aveia", "banana"])
        self.assertEqual(extract_grocery_items("preciso comprar frango"), ["frango"])
        self.assertEqual(extract_grocery_items("oi, tudo bem?"), [])
        self.assertLessEqual(len(extract_grocery_items("a, b, c, d, e, f, g, h, i, j, k, l")), 10)

    def test_closing_never_lies(self):
        for action in ACTION_CATALOG:
            fala = closing_fala(action, {"items": ["aveia"], "sistolica": 120,
                                         "diastolica": 80, "glicemia": 100,
                                         "quantidade_ml": 250})
            for banned in ("Já registrei", "Já salvei", "Já abri", "Já anotei", "registrado com"):
                self.assertNotIn(banned, fala)
        self.assertIn("aveia", closing_fala("ABRIR_LISTA_COMPRAS", {"items": ["aveia"]}))
        self.assertEqual(closing_fala("NOPE", {}), "Pronto! Confirma na tela?")


if __name__ == "__main__":
    unittest.main()
