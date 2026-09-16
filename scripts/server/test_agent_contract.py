"""Testes do contrato do agente B0."""
import unittest

from curate_dataset import audit
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


class CurateContractTests(unittest.TestCase):
    def _entry(self, user, asst, action, source):
        return {
            "id": "t", "timestamp": "2026-09-01", "source": source, "category": "x",
            "messages": [{"role": "user", "content": user},
                         {"role": "assistant", "content": asst}],
            "metadata": {"action": action, "params": {}},
        }

    def test_chat_registration_flagged(self):
        v, r = audit(self._entry("pressão 12 por 8", "Entendi: 120 por 80.",
                                 "REGISTRAR_PRESSAO", "app_chat"))
        self.assertEqual(v, "quarantine")
        self.assertTrue(any(x.startswith("action-sem-executor") for x in r))

    def test_whatsapp_registration_kept(self):
        v, _ = audit(self._entry("pressão 12 por 8", "Entendi: 120 por 80.",
                                 "REGISTRAR_PRESSAO", "whatsapp"))
        self.assertEqual(v, "keep")

    def test_chat_list_ok(self):
        v, _ = audit(self._entry("adiciona aveia", "Preparei sua lista com aveia.",
                                 "ABRIR_LISTA_COMPRAS", "app_chat"))
        self.assertEqual(v, "keep")

    def test_fastlane_untouched(self):
        v, _ = audit(self._entry("bom dia", "Bom dia!", "FAST_LANE", "whatsapp_fastlane"))
        self.assertEqual(v, "keep")


if __name__ == "__main__":
    unittest.main()
