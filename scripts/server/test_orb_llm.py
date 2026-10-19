"""Testes das funções puras do two-step LLM (Fase 3). Run with unittest."""
import json
import unittest

from orb_llm import (
    CLASSIFY_PROMPT,
    SPEAK_SYSTEM,
    assistant_fala,
    build_classify_payload,
    build_speak_payload,
    compose_envelope,
    metricas_fala,
    parse_label,
)


class ParseLabelTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(parse_label("CONVERSA"), "CONVERSA")
        self.assertEqual(parse_label("EMERGENCIA"), "EMERGENCIA")

    def test_channel_gates_labels(self):
        # B1: rótulo sem executor no canal vira CONVERSA.
        self.assertEqual(parse_label("ABRIR_LISTA_COMPRAS", channel="chat"), "ABRIR_LISTA_COMPRAS")
        self.assertEqual(parse_label("ABRIR_LISTA_COMPRAS", channel="whatsapp"), "CONVERSA")
        self.assertEqual(parse_label("REGISTRAR_PRESSAO", channel="chat"), "CONVERSA")
        self.assertEqual(parse_label("REGISTRAR_PRESSAO", channel="whatsapp"), "CONVERSA")

    def test_embedded_sentence(self):
        self.assertEqual(parse_label("A resposta é LEMBRETES."), "LEMBRETES")
        self.assertEqual(parse_label("acho que EXAMES"), "EXAMES")

    def test_priority(self):
        self.assertEqual(parse_label("CONVERSA ou EMERGENCIA"), "EMERGENCIA")

    def test_safe_defaults(self):
        for text in ("", "   ", None, '{"fala": "oi"}', "banana", "CONVERSAR"):
            with self.subTest(text=text):
                self.assertEqual(parse_label(text), "CONVERSA")

    def test_no_substring_ghost(self):
        # "CONVERSAR" não é rótulo; "EXAME" singular não é EXAMES.
        self.assertEqual(parse_label("quero conversar"), "CONVERSA")


class PayloadTests(unittest.TestCase):
    def test_classify_is_cheap(self):
        p = build_classify_payload("m", "oi")
        self.assertEqual(p["max_tokens"], 12)
        self.assertEqual(p["temperature"], 0)
        self.assertIn("CONVERSA", p["messages"][0]["content"])
        self.assertIn(CLASSIFY_PROMPT, p["messages"][0]["content"])

    def test_classify_lists_channel_executors(self):
        chat_p = build_classify_payload("m", "oi", channel="chat")
        self.assertIn("ABRIR_LISTA_COMPRAS", chat_p["messages"][0]["content"])
        wa_p = build_classify_payload("m", "oi", channel="voice")
        self.assertIn("REGISTRAR_PRESSAO", wa_p["messages"][0]["content"])
        self.assertNotIn("ABRIR_LISTA_COMPRAS", wa_p["messages"][0]["content"])

    def test_speak_has_no_json_instruction(self):
        p = build_speak_payload("m", [{"role": "user", "content": "oi"}], 0.1, 220)
        system = p["messages"][0]["content"]
        self.assertIn("Braga", system)
        self.assertNotIn("acao", system)
        self.assertNotIn("parametros", system)
        self.assertEqual(p["messages"][1]["content"], "oi")


class TemplateTests(unittest.TestCase):
    def test_metricas_full_row(self):
        fala = metricas_fala({"systolic_pressure": 120, "diastolic_pressure": 80, "glucose_level": 100})
        self.assertIn("120", fala)
        self.assertIn("80", fala)
        self.assertIn("100", fala)

    def test_metricas_partial_row(self):
        fala = metricas_fala({"systolic_pressure": None, "diastolic_pressure": None, "glucose_level": 90})
        self.assertIn("90", fala)

    def test_metricas_empty(self):
        for row in (None, {}, {"systolic_pressure": None, "diastolic_pressure": None, "glucose_level": None}):
            with self.subTest(row=row):
                self.assertIn("relatório", metricas_fala(row))

    def test_envelope_roundtrip(self):
        env = compose_envelope("Olá!", "CONVERSA", {})
        parsed = json.loads(env)
        self.assertEqual((parsed["fala"], parsed["acao"], parsed["parametros"]), ("Olá!", "CONVERSA", {}))

    def test_speak_system_is_short(self):
        # Orçamento inclui a personalidade compartilhada e o contrato do canal.
        self.assertLess(len(SPEAK_SYSTEM), 3000)


class AssistantFalaTests(unittest.TestCase):
    def test_strips_old_envelope(self):
        env = json.dumps({"fala": "Olá, tudo bem?", "acao": "CONVERSA", "parametros": {}})
        self.assertEqual(assistant_fala(env), "Olá, tudo bem?")

    def test_plain_passthrough(self):
        self.assertEqual(assistant_fala("resposta simples"), "resposta simples")
        self.assertEqual(assistant_fala('{"incompleto'), '{"incompleto')
        self.assertEqual(assistant_fala('{"fala": "  "}'), '{"fala": "  "}')


if __name__ == "__main__":
    unittest.main()
