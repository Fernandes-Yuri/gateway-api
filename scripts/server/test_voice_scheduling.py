"""Testes do agendamento de consulta por voz no modo cuidador (D50).

A extracao de data/hora e deterministica (sem LLM) justamente para nao
alucinar agenda. Aqui cobrimos: intencao, especialidade, dia da semana,
prazo relativo, dia do mes, hora solta e os casos fail-closed.
"""
import unittest
from datetime import datetime, timezone

from voice_scheduling import (
    quer_agendar,
    extrair_especialidade,
    extrair_data_hora,
)

# Sexta-feira, 18 de setembro de 2026, 10:00 UTC
# weekdir(): segunda=0 ... sexta=4
AGORA = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


class IntencaoTests(unittest.TestCase):
    def test_detecta_agendar_consulta(self):
        self.assertTrue(quer_agendar("Agenda uma consulta com o cardiologista"))
        self.assertTrue(quer_agendar("marca uma consulta pro ortopedista"))
        self.assertTrue(quer_agendar("quero marcar no médico"))

    def test_ignora_conversa_comum(self):
        self.assertFalse(quer_agendar("Como está a pressão da minha mãe?"))
        self.assertFalse(quer_agendar("Bom dia"))
        self.assertFalse(quer_agendar(""))
        # "consulta" sem intencao de agendar
        self.assertFalse(quer_agendar("Quando foi a ultima consulta?"))


class EspecialidadeTests(unittest.TestCase):
    def test_mapeia(self):
        self.assertEqual(extrair_especialidade("cardiologista"), "Cardiologista")
        self.assertEqual(extrair_especialidade("clinico geral"), "Clínico geral")
        self.assertEqual(extrair_especialidade("no UBS"), "Posto de saúde (UBS)")

    def test_padrao(self):
        self.assertEqual(extrair_especialidade("qualquer coisa"), "Consulta médica")


class DataHoraTests(unittest.TestCase):
    def test_dia_da_semana_com_hora(self):
        # "quinta-feira às 14h" -> quinta = offset 3, hoje sexta -> 3 dias
        dt, s = extrair_data_hora("quinta-feira às 14h", AGORA)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.weekday(), 3)  # quinta
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 0)

    def test_proxima_semana(self):
        # "proxima semana as 9h": 7 dias adiante, mas a hora 9h < 10h (agora)
        # faz (dt-agora) dar ~6.96 dias — a data correta e o mesmo dia, 9h.
        dt, _ = extrair_data_hora("agenda para proxima semana as 9h", AGORA)
        self.assertIsNotNone(dt)
        alvo = AGORA + __import__("datetime").timedelta(days=7)
        self.assertEqual(dt.date(), alvo.date())
        self.assertEqual(dt.hour, 9)

    def test_amanha(self):
        dt, _ = extrair_data_hora("amanha as 15h30", AGORA)
        self.assertIsNotNone(dt)
        self.assertEqual((dt - AGORA).days, 1)
        self.assertEqual(dt.hour, 15)
        self.assertEqual(dt.minute, 30)

    def test_dia_do_mes_com_mes(self):
        dt, _ = extrair_data_hora("dia 20 de outubro as 8h", AGORA)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.day, 20)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.hour, 8)

    def test_hora_minuto_com_dois_pontos(self):
        dt, _ = extrair_data_hora("as 14:30", AGORA)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 30)

    def test_hora_no_passado_vira_amanha(self):
        # agora sao 10h; "as 8h" ja passou -> amanha 8h
        dt, _ = extrair_data_hora("as 8h", AGORA)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 8)
        self.assertGreater(dt, AGORA)

    def test_falha_sem_data_nem_hora(self):
        # fail-closed: so diz "agenda consulta" sem quando
        dt, s = extrair_data_hora("agenda uma consulta", AGORA)
        self.assertIsNone(dt)

    def test_falha_hora_invalida(self):
        dt, _ = extrair_data_hora("as 99h", AGORA)
        self.assertIsNone(dt)


if __name__ == "__main__":
    unittest.main()
