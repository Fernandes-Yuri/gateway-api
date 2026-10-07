"""Regressões de significado e autorização, sem dados do dataset congelado."""
import unittest
from orb_router import route_chat, whatsapp_next
from orb_semantics import beverage_mentions, is_current_emergency, validate_envelope, validated_label


class SemanticTests(unittest.TestCase):
    def test_negated_consumption_and_cancel_do_not_become_water(self):
        for raw in ("Não bebi 500 ml de água", "Não anota 500 ml de água", "nunca bebi 500 ml de suco"):
            with self.subTest(raw=raw):
                mention = beverage_mentions(raw)[0]
                self.assertTrue(mention["negated"])
                self.assertEqual(mention["quantity_ml"], 500)
                response = route_chat(raw)
                self.assertEqual(response["acao"], "CONVERSA")
                self.assertNotIn("Boa!", response["fala"])
                self.assertEqual(whatsapp_next(raw, True)["type"], "text")

    def test_beverage_entity_and_quantity_survive(self):
        for beverage in ("suco", "chá", "café", "leite", "refrigerante"):
            raw = f"bebi 500 ml de {beverage}"
            response = route_chat(raw)
            self.assertIn(f"500 ml de {beverage}", response["fala"])
            self.assertNotIn("ml de água", response["fala"])
            self.assertEqual(whatsapp_next(raw, True)["type"], "text")
        self.assertEqual(beverage_mentions("bebi 0,5 litro de água")[0]["quantity_ml"], 500)
        self.assertEqual(whatsapp_next("bebi 500 ml de água", True)["metric"], "AGUA")

    def test_emergency_educational_denial_history_and_current_report(self):
        for raw in ("o que significa dor no peito?", "não estou com dor no peito", "ontem tive dor no peito",
                    "se eu tiver falta de ar, o que faço?", "como prevenir infarto?"):
            self.assertFalse(is_current_emergency(raw), raw)
            self.assertEqual(validated_label(raw, "EMERGENCIA"), "CONVERSA")
            response = route_chat(raw)
            self.assertTrue(response is None or response["acao"] != "EMERGENCIA")
        for raw in ("por que estou com dor no peito?", "por que não consigo respirar?",
                    "não tenho dor no peito, mas não consigo respirar", "ontem tive dor no peito e agora voltou",
                    "como excluo minha conta? Estou com dor no peito", "registre minha refeição, estou com falta de ar",
                    "chame o SAMU", "meu pai está com dor no peito"):
            self.assertTrue(is_current_emergency(raw), raw)
            self.assertEqual(route_chat(raw)["acao"], "EMERGENCIA")

    def test_every_remote_action_needs_authorization_and_matching_values(self):
        raw = "o que significa dor no peito?"
        for action in ("EMERGENCIA", "EXAMES", "LEMBRETES", "REGISTRAR_AGUA", "ABRIR_LISTA_COMPRAS", "INVENTADA"):
            response = validate_envelope(raw, {"fala": "Já abri as opções", "acao": action, "parametros": {"quantidade_ml": 500}})
            self.assertEqual(response["acao"], "CONVERSA")
            self.assertEqual(response["parametros"], {})
            self.assertNotIn("Já abri", response["fala"])
        self.assertEqual(validated_label("não abra meus exames", "EXAMES"), "CONVERSA")
        self.assertEqual(validated_label("quero ver meus exames", "EXAMES"), "EXAMES")
        for raw in ("não bebi 500 ml de água", "bebi 500 ml de suco", "bebi 500 ml"):
            self.assertEqual(validate_envelope(raw, {"fala": "Registrando", "acao": "REGISTRAR_AGUA", "parametros": {"quantidade_ml": 500}})["acao"], "CONVERSA")
        self.assertEqual(validate_envelope("bebi 500 ml de água", {"fala": "Confira", "acao": "REGISTRAR_AGUA", "parametros": {"quantidade_ml": 1000}})["acao"], "CONVERSA")

    def test_negation_and_recurrence_are_scoped_to_their_clauses(self):
        self.assertTrue(is_current_emergency('Ontem tive dor no peito e agora voltou a doer forte'))
        self.assertTrue(is_current_emergency('não tenho dor no peito, estou com falta de ar'))
        self.assertFalse(is_current_emergency('o que significa dor no peito? Minha dor de cabeça voltou'))
        mentions = beverage_mentions('não bebi 500 ml de água, bebi 250 ml de suco')
        self.assertEqual(len(mentions), 2)
        self.assertTrue(mentions[0]['negated'])
        self.assertFalse(mentions[1]['negated'])
        self.assertEqual(mentions[1]['beverage'], 'suco')
        self.assertEqual(mentions[1]['quantity_ml'], 250)
        self.assertIsNone(route_chat('não bebi água, mas por que minha pressão subiu?'))

    def test_educational_water_is_not_a_consumption_statement(self):
        for raw in ("o que significa beber água?", "quanto devo beber de água?", "não consumi 500 ml de água"):
            response = route_chat(raw)
            self.assertTrue(response is None or 'Boa!' not in response['fala'])
            self.assertNotEqual(whatsapp_next(raw, True)['type'], 'vitals')

    def test_conversation_cannot_claim_unexecuted_actions(self):
        for fala in ('Já abri as opções de socorro.', 'Salvei 500 ml de água.', 'Entendi, já avisei seus contatos.'):
            response = validate_envelope('o que significa dor no peito?', {'fala': fala, 'acao': 'CONVERSA', 'parametros': {}})
            self.assertNotEqual(response['fala'], fala)
            self.assertEqual(response['acao'], 'CONVERSA')

    def test_client_hints_can_only_restrict(self):
        raw = "quero ver meus exames"
        context = {"original_text": raw, "conversation_only": True}
        self.assertEqual(route_chat(raw, context)["acao"], "CONVERSA")
        self.assertEqual(validated_label(raw, "EXAMES", context), "CONVERSA")
        self.assertEqual(validated_label("o que significa dor no peito?", "EMERGENCIA", {"emergency": True}), "CONVERSA")
        self.assertEqual(route_chat(raw, {"original_text": "outra entrada", "conversation_only": True})["acao"], "EXAMES")
