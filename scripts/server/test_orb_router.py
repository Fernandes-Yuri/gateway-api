"""Suite do roteador determinístico (Fase 2). Run with unittest."""
import unittest

from orb_router import (
    dedupe_fala,
    detect_all,
    recall,
    remember,
    resolve_followup,
    route_chat,
)


class RouterTests(unittest.TestCase):
    def assertAction(self, text, action):
        r = route_chat(text)
        self.assertIsNotNone(r, f"esperava rota para: {text!r}")
        self.assertEqual(r["acao"], action, f"texto: {text!r} -> {r}")
        self.assertEqual(r["parametros"], {})
        self.assertTrue(r["fala"].strip())
        return r

    def assertEcho(self, text, *numbers):
        r = self.assertAction(text, "CONVERSA")
        for n in numbers:
            self.assertIn(str(n), r["fala"], f"numero {n} ausente em: {r['fala']!r}")

    # ---- Pressão ----
    def test_bp_short(self):
        self.assertEcho("pressão 12 por 8", 120, 80)

    def test_bp_slash(self):
        self.assertEcho("minha pressão deu 120/80", 120, 80)

    def test_bp_half(self):
        self.assertEcho("tô com 13 por 8 e meio", 130, 85)

    def test_bp_words(self):
        self.assertEcho("doze por oito", 120, 80)

    def test_bp_compact_x_from_chat(self):
        self.assertEcho("MINHA PRESSÃO DEU 12X8", 120, 80)

    def test_bp_high(self):
        r = self.assertAction("pressao 16 por 10", "CONVERSA")
        self.assertIn("160", r["fala"])

    def test_bp_implausible_safe(self):
        # Valor impossível: cai em tópico clínico que agora vai pro LLM
        self.assertIsNone(route_chat("pressão 300 por 200"))

    # ---- Glicemia ----
    def test_glucose(self):
        self.assertEcho("glicemia 105", 105)

    def test_glucose_suffix(self):
        self.assertEcho("105 de glicose", 105)

    def test_glucose_low(self):
        r = self.assertAction("minha glicose deu 65", "CONVERSA")
        self.assertIn("65", r["fala"])

    def test_glucose_topic_no_number(self):
        self.assertIsNone(route_chat("estou com dúvida sobre diabetes"))

    # ---- Hidratação ----
    def test_water_cups(self):
        r = self.assertAction("tomei dois copos de água", "CONVERSA")
        self.assertIn("500", r["fala"])

    def test_water_ml(self):
        self.assertEcho("bebi 500 ml de água", 500)

    def test_water_bare(self):
        r = self.assertAction("bebi água", "CONVERSA")
        self.assertIn("Quanto", r["fala"])
        self.assertNotIn("250", r["fala"])

    # ---- FC / SpO2 ----
    def test_hr(self):
        self.assertEcho("meus batimentos estão 72", 72)

    def test_spo2(self):
        self.assertEcho("saturação 97", 97)

    # ---- Emergência ----
    def test_emergency_chest(self):
        self.assertAction("estou com dor no peito", "EMERGENCIA")

    def test_emergency_breath(self):
        self.assertAction("não consigo respirar, falta de ar", "EMERGENCIA")

    def test_emergency_faint(self):
        self.assertAction("quase desmaiei agora", "EMERGENCIA")

    # ---- Lembretes / exames ----
    def test_reminder(self):
        self.assertAction("me lembra do remédio das 8", "LEMBRETES")

    def test_reminder_word(self):
        self.assertAction("abrir meus lembretes", "LEMBRETES")

    def test_exams(self):
        self.assertAction("quero ver meus exames", "EXAMES")

    def test_med_topic(self):
        self.assertEqual(detect_all("tomei meu remédio da pressão")["kind"], "med_take")

    def test_medication_history_question_precedes_take_intent(self):
        self.assertEqual(detect_all("Ele tomou o remédio hoje de manhã?")["kind"], "query_medication")
        self.assertEqual(detect_all("Quero saber se o Yuri tomou o remédio hoje de manhã")["kind"], "query_medication")
        self.assertEqual(detect_all("Tomei meu remédio hoje")["kind"], "med_take")
        self.assertEqual(detect_all("Qual remédio devo tomar?")["kind"], "meds")

    def test_explicit_code_requests_are_blocked_before_llm(self):
        for text in (
            "escreva um hello world em Python",
            "faça um programa em Python, uma calculadora padrão",
            "conte de 1 até 1000",
            "escreva um livro",
        ):
            self.assertEqual(detect_all(text)["kind"], "out_of_scope")
            self.assertAction(text, "CONVERSA")
            self.assertIn("programação", route_chat(text)["fala"])
        self.assertNotEqual(detect_all("Meu filho estuda Python")["kind"], "out_of_scope")
        self.assertNotEqual(detect_all("conte de 1 até 10")["kind"], "out_of_scope")

    def test_model_identity_answers_with_configured_model(self):
        self.assertEqual(detect_all("qual modelo de inteligência artificial você é")["kind"], "model_identity")
        self.assertIn("qwen-test", route_chat("qual modelo de inteligência artificial você é", model_name="qwen-test")["fala"])

    # ---- Nutrição sem fantasma ----
    def test_meal_mention(self):
        self.assertIn("descontinuado", route_chat("almocei arroz e feijão")["fala"])

    def test_nutrition_question(self):
        self.assertIn("descontinuado", route_chat("o que posso almoçar?")["fala"])

    # ---- Social (passam ao LLM para resposta calorosa e humana) ----
    def test_good_morning(self):
        self.assertIsNone(route_chat("bom dia"))
        self.assertEqual(detect_all("bom dia")["kind"], "greeting_morning")

    def test_good_evening(self):
        self.assertIsNone(route_chat("boa noite, vou dormir"))
        self.assertEqual(detect_all("boa noite, vou dormir")["kind"], "greeting_evening")

    def test_hi(self):
        self.assertIsNone(route_chat("oi, tudo bem?"))
        self.assertEqual(detect_all("oi, tudo bem?")["kind"], "greeting")

    def test_thanks(self):
        self.assertIsNone(route_chat("obrigado pela ajuda"))
        self.assertEqual(detect_all("obrigado pela ajuda")["kind"], "gratitude")

    def test_app_link(self):
        self.assertAction("onde baixo o app?", "CONVERSA")

    def test_identity(self):
        self.assertAction("quem é você?", "CONVERSA")

    def test_time_query(self):
        r = self.assertAction("que horas são?", "CONVERSA")
        self.assertIn("horas", r["fala"])

    def test_date_query(self):
        r = self.assertAction("que dia é hoje?", "CONVERSA")
        self.assertIn("Hoje é", r["fala"])

    def test_capabilities(self):
        self.assertAction("o que você faz?", "CONVERSA")

    def test_capabilities_capaz(self):
        det = detect_all("eu preciso saber o que você é capaz de fazer")
        self.assertEqual(det["kind"], "identity")

    # ---- Tópico clínico sem número ----
    def test_topic_pressure(self):
        self.assertIsNone(route_chat("minha pressão anda estranha"))

    def test_topic_pain(self):
        self.assertAction("estou com dor de cabeça", "CONVERSA")

    # ---- FANTASMAS DE DIETA: fofoca cotidiana é smalltalk e passa ao LLM ----
    def test_ghost_correria(self):
        self.assertIsNone(route_chat("tô na correria aqui"))
        self.assertEqual(detect_all("tô na correria aqui")["kind"], "smalltalk")

    def test_ghost_descer(self):
        self.assertIsNone(route_chat("vou descer ali rapidinho"))
        self.assertEqual(detect_all("vou descer ali rapidinho")["kind"], "smalltalk")

    def test_ghost_ok(self):
        self.assertIsNone(route_chat("beleza então"))
        self.assertEqual(detect_all("beleza então")["kind"], "smalltalk")

    def test_ghost_back(self):
        self.assertIsNone(route_chat("vou ali e já volto"))
        self.assertEqual(detect_all("vou ali e já volto")["kind"], "smalltalk")

    def test_ghost_laugh(self):
        self.assertIsNone(route_chat("haha boa"))
        self.assertEqual(detect_all("haha boa")["kind"], "smalltalk")

    def test_ghost_greeting_bait(self):
        # "bom dia, vou descer ali" tem saudação -> passa ao LLM, nunca dieta
        self.assertIsNone(route_chat("bom dia, vou descer ali rapidinho"))
        self.assertEqual(detect_all("bom dia, vou descer ali rapidinho")["kind"], "greeting_morning")

    # ---- Conversa aberta real vai ao LLM ----
    def test_open_sky(self):
        self.assertIsNone(route_chat("por que o céu é azul?"))

    def test_open_story(self):
        self.assertIsNone(route_chat("me conta uma história longa sobre o mar e os pescadores da vila"))

    def test_open_opinion(self):
        self.assertIsNone(route_chat("o que você acha sobre aposentadoria nos dias de hoje?"))

    def test_open_long_statement(self):
        self.assertIsNone(route_chat(
            "hoje eu acordei cedo, caminhei no parque com meu vizinho e depois fomos tomar café na padaria da esquina onde sempre conversamos sobre futebol"))

    def test_empty(self):
        self.assertIsNone(route_chat(""))
        self.assertIsNone(route_chat("   "))


class DetectAllTests(unittest.TestCase):
    def test_kinds_and_values(self):
        self.assertEqual(detect_all("pressão 12 por 8"), {"kind": "bp", "sys": 120, "dia": 80})
        self.assertEqual(detect_all("glicemia 105"), {"kind": "glucose", "valor": 105})
        self.assertEqual(detect_all("dois copos de água"), {"kind": "hydration", "ml": 500})
        self.assertEqual(detect_all("batimentos 72"), {"kind": "hr", "bpm": 72})
        self.assertEqual(detect_all("saturação 97"), {"kind": "spo2", "valor": 97})
        self.assertEqual(detect_all("dor no peito")["kind"], "emergency")
        self.assertEqual(detect_all("bom dia")["kind"], "greeting_morning")
        self.assertEqual(detect_all("tô na correria aqui")["kind"], "smalltalk")
        self.assertEqual(detect_all("por que o céu é azul?")["kind"], "open")
        self.assertEqual(detect_all("")["kind"], "empty")

    def test_gi_symptoms_get_guidance(self):
        for text in ("estou com diarreia desde cedo", "fazendo cocô mole",
                     "vomitei agora", "estou com náusea", "azia forte"):
            with self.subTest(text=text):
                det = detect_all(text)
                self.assertEqual(det["kind"], "symptom_gi", text)
                env = route_chat(text)
                self.assertEqual(env["acao"], "CONVERSA")
                self.assertIn("UPA", env["fala"])
                if "vomit" in text:
                    self.assertNotIn("diarreia", env["fala"])

    def test_headache_and_fever_guidance(self):
        env = route_chat("tô com dor de cabeça também")
        self.assertEqual(env["acao"], "CONVERSA")
        self.assertIn("calmo", env["fala"])
        env = route_chat("estou com febre")
        self.assertIn("repouso", env["fala"])

    def test_symptom_repeat_escalates(self):
        from orb_router import symptom_repeat, ESCALATION_SUFFIX
        mem = {}
        self.assertEqual(symptom_repeat(mem, "u", "symptom_gi", now=1000.0), 1)
        self.assertEqual(symptom_repeat(mem, "u", "symptom_gi", now=1100.0), 2)
        self.assertEqual(symptom_repeat(mem, "u", "symptom_gi", now=2000.0), 1)  # expirou
        self.assertIn("UBS", ESCALATION_SUFFIX)

    def test_bot_directed_question(self):
        det = detect_all("você tem dor de barriga?")
        self.assertEqual(det["kind"], "bot")
        env = route_chat("você tem dor de barriga?")
        self.assertEqual(env["acao"], "CONVERSA")
        self.assertIn("assistente virtual", env["fala"])

    def test_self_service_wording(self):
        # Chat orienta o auto-registro; voz é o agente auxiliar.
        for text in ("pressão 12 por 8", "glicemia 100"):
            with self.subTest(text=text):
                env = route_chat(text)
                self.assertIn("Você pode", env["fala"])
                self.assertIn("assistente de voz", env["fala"])

    def test_grocery_retirement_does_not_offer_an_action(self):
        env = route_chat("abre minha lista de compras")
        self.assertEqual(env["acao"], "CONVERSA")
        self.assertIn("descontinuado", env["fala"])
        self.assertEqual(env["parametros"], {})


class DedupeTests(unittest.TestCase):
    def test_varies_on_repeat(self):
        mem = {}
        first = dedupe_fala(mem, "u", "Olá!")
        second = dedupe_fala(mem, "u", "Olá!")
        self.assertEqual(first, "Olá!")
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("Olá!"))

    def test_resets_on_change(self):
        mem = {}
        dedupe_fala(mem, "u", "A")
        self.assertEqual(dedupe_fala(mem, "u", "B"), "B")

    def test_route_chat_matches_detect_all(self):
        # Kind determinístico tem envelope; open/empty/social dão None para LLM.
        samples = ["pressão 12 por 8", "glicemia 100", "bebi água",
                   "me lembra do remédio", "quero ver exames",
                   "dor de cabeça"]
        for text in samples:
            with self.subTest(text=text):
                det = detect_all(text)
                env = route_chat(text)
                self.assertIsNotNone(env, text)
                self.assertEqual(env["parametros"], {})
                if det["kind"] == "emergency":
                    self.assertEqual(env["acao"], "EMERGENCIA")
                else:
                    self.assertEqual(env["acao"], "CONVERSA" if det["kind"] not in ("reminder", "exams") else env["acao"])


class MemoryTests(unittest.TestCase):
    def test_remember_recall_expire(self):
        mem = {}
        remember(mem, "k", "nutrition", now=1000.0)
        self.assertEqual(recall(mem, "k", now=1500.0), "nutrition")
        self.assertIsNone(recall(mem, "k", now=1700.0))  # TTL 600
        self.assertIsNone(recall(mem, "missing", now=1500.0))


class FollowupTests(unittest.TestCase):
    def test_ate_chat(self):
        env = resolve_followup("já comi", "nutrition", channel="chat")
        self.assertIsNotNone(env)
        self.assertEqual(env["acao"], "CONVERSA")

    def test_grocery_wa(self):
        env = resolve_followup("põe na lista", "nutrition", channel="wa")
        self.assertIsNotNone(env)
        self.assertIn("descontinuado", env["fala"].lower())

    def test_ate_item_quote(self):
        # Usuário quotando o template de volta ("anotar item por item").
        env = resolve_followup("entendi, você consegue anotar aí item por item", "nutrition", channel="chat")
        self.assertIsNotNone(env)
        self.assertIn("descontinuado", env["fala"])

    def test_explicit_list_wins(self):
        env = resolve_followup("anota na lista", "nutrition", channel="chat")
        self.assertIsNotNone(env)
        self.assertIn("descontinuado", env["fala"].lower())

    def test_no_memory_no_followup(self):
        self.assertIsNone(resolve_followup("já comi", None))
        self.assertIsNone(resolve_followup("já comi", "smalltalk"))
        self.assertIsNone(resolve_followup("sim", "nutrition"))  # ambíguo: cai no smalltalk
        self.assertIsNone(resolve_followup("", "nutrition"))




if __name__ == "__main__":
    unittest.main()
