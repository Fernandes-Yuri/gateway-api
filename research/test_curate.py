"""Testes da curadoria (Fase 5). Run with unittest."""
import unittest

from curate_dataset import audit


def new_entry(user, asst, action="CONVERSA", params=None, source="app_chat"):
    return {
        "id": "t", "timestamp": "2026-09-01", "source": source, "category": "general_care",
        "messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": asst},
        ],
        "metadata": {"action": action, "params": params or {}},
    }


class AuditTests(unittest.TestCase):
    def test_false_confirmation(self):
        v, r = audit(new_entry(
            "Acabei de medir minha pressão agora e deu 12 por 8.",
            '{"fala": "Já registrei com carinho na sua rotina!", "acao": "REGISTRAR_PRESSAO"}',
            action="REGISTRAR_PRESSAO", params={"sistolica": 120, "diastolica": 80}))
        self.assertEqual(v, "quarantine")
        self.assertIn("false-confirmation", r)

    def test_invented_numbers(self):
        v, r = audit(new_entry(
            "como está minha pressão?",
            '{"fala": "Sua pressão está 130 por 85.", "acao": "CONVERSA"}',
            action="CONVERSA", params={"sistolica": 130}))
        self.assertEqual(v, "quarantine")
        self.assertTrue(any(x.startswith("invented-numbers") for x in r))

    def test_scale_short_allowed(self):
        v, _ = audit(new_entry(
            "pressão 12 por 8",
            '{"fala": "Entendi: 120 por 80.", "acao": "CONVERSA"}',
            action="CONVERSA", params={}))
        self.assertEqual(v, "keep")

    def test_diet_ghost(self):
        v, r = audit(new_entry(
            "tô na correria aqui",
            '{"fala": "Que tal uma salada com frango?", "acao": "CONVERSA"}'))
        self.assertEqual(v, "quarantine")
        self.assertTrue(any(x.startswith("diet-ghost") for x in r))

    def test_explicit_food_is_clean(self):
        v, _ = audit(new_entry(
            "Banana com aveia é bom para o lanche?",
            '{"fala": "A banana com aveia tem fibras.", "acao": "CONVERSA"}'))
        self.assertEqual(v, "keep")

    def test_legacy_variants(self):
        for entry in ({"prompt_usuario": "Oi", "resposta_braga": "Olá!"},
                      {"timestamp": "x", "prompt": "Oi", "response": "Olá!"}):
            with self.subTest(entry=entry):
                v, r = audit(entry)
                self.assertEqual(v, "quarantine")
                self.assertIn("legacy-unconverted", r)

    def test_explicit_nutrition_question_not_ghost(self):
        v, _ = audit(new_entry(
            "O que posso almoçar?",
            '{"fala": "Opte por refeição com proteínas e vegetais.", "acao": "CONVERSA"}'))
        self.assertEqual(v, "keep")

    def test_router_template_clean(self):
        v, _ = audit(new_entry(
            "tô na correria aqui",
            "Entendi! Estou por aqui. Quer falar de pressão, glicemia, água, remédios ou outra coisa da sua rotina?",
            source="whatsapp_router"))
        self.assertEqual(v, "keep")


class CurateContractTests(unittest.TestCase):
    """Contrato do agente B0 do ponto de vista da curadoria (movido de scripts/server)."""

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
