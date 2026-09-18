"""Testes do agendamento de consulta por voz no modo cuidador (D50/D51).

A extracao de data/hora e deterministica (sem LLM) justamente para nao
alucinar agenda. Aqui cobrimos: intencao, especialidade, dia da semana,
prazo relativo, dia do mes, hora solta e os casos fail-closed.

D51: o fluxo completo de rascunho + confirmacao (por voz e por botao),
correcao por voz e cancelamento.
"""
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from voice_scheduling import (
    quer_agendar,
    quer_confirmar,
    quer_corrigir,
    quer_cancelar,
    extrair_especialidade,
    extrair_data_hora,
    agendar_por_voz,
    confirmar_rascunho_por_id,
    obter_rascunho,
    descartar_rascunho,
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


class IntencaoConfirmarTests(unittest.TestCase):
    def test_detecta_confirmar(self):
        self.assertTrue(quer_confirmar("eu confirmo"))
        self.assertTrue(quer_confirmar("pode confirmar"))
        self.assertTrue(quer_confirmar("Sim, confirmar!"))
        self.assertTrue(quer_confirmar("confirmado"))

    def test_nao_confirma_negando(self):
        self.assertFalse(quer_confirmar("não confirmo"))
        self.assertFalse(quer_confirmar("não quero confirmar"))

    def test_correcao_nao_e_confirmacao(self):
        # "na verdade é confirmar" seria correcao, nao confirmacao
        self.assertFalse(quer_confirmar("na verdade é sexta"))
        self.assertFalse(quer_confirmar("errado, é dia 25"))

    def test_cancelamento_nao_e_confirmacao(self):
        self.assertFalse(quer_confirmar("cancela"))

    def test_corrigir(self):
        self.assertTrue(quer_corrigir("na verdade é sexta-feira"))
        self.assertTrue(quer_corrigir("errado, é dia 25"))
        self.assertFalse(quer_corrigir("está certo"))

    def test_cancelar(self):
        self.assertTrue(quer_cancelar("cancela isso"))
        self.assertTrue(quer_cancelar("esquece"))
        self.assertFalse(quer_cancelar("quero agendar"))


class RascunhoFluxoTests(unittest.TestCase):
    """D51: rascunho + confirmacao — nada e gravado antes do 'confirmo'."""

    UID = "cuidador_teste"
    PID = "paciente_teste"

    def _conn(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=True)  # require_family_access OK
        conn.fetchrow = AsyncMock(return_value={
            "id": "consulta-123",
            "title": "Cardiologista",
            "scheduled_date": datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc),
        })
        return conn

    def setUp(self):
        descartar_rascunho(self.UID, self.PID)

    def tearDown(self):
        descartar_rascunho(self.UID, self.PID)

    def test_agendar_cria_rascunho_sem_gravar(self):
        conn = self._conn()
        env = asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        # Rascunho, NAO consulta agendada
        self.assertEqual(env["acao"], "AGENDAR_RASCUNHO")
        draft = obter_rascunho(self.UID, self.PID)
        self.assertIsNotNone(draft)  # rascunho existe...
        self.assertEqual(draft["title"], "Cardiologista")
        # ...mas nada foi gravado na agenda ainda
        conn.fetchrow.assert_not_called()
        self.assertIn("Deseja confirmar", env["fala"])

    def test_confirmar_por_voz_grava(self):
        conn = self._conn()
        asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        env = asyncio.run(agendar_por_voz(conn, self.UID, self.PID, "eu confirmo"))
        self.assertEqual(env["acao"], "CONSULTA_AGENDADA")
        self.assertEqual(env["parametros"]["title"], "Cardiologista")
        # o INSERT rodou com o patient_id correto
        self.assertIn(self.PID, str(conn.fetchrow.call_args))
        # rascunho consumido
        self.assertIsNone(obter_rascunho(self.UID, self.PID))

    def test_confirmar_sem_rascunho_nao_grava(self):
        conn = self._conn()
        env = asyncio.run(agendar_por_voz(conn, self.UID, self.PID, "eu confirmo"))
        self.assertEqual(env["acao"], "AGENDAR_FALHA")
        conn.fetchrow.assert_not_called()

    def test_correcao_por_voz_refaz_rascunho(self):
        conn = self._conn()
        asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        r1 = obter_rascunho(self.UID, self.PID)
        # corrige a especialidade; a hora vem do rascunho anterior
        env = asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID, "na verdade é com o ortopedista"))
        self.assertEqual(env["acao"], "AGENDAR_RASCUNHO")
        r2 = obter_rascunho(self.UID, self.PID)
        self.assertEqual(r2["title"], "Ortopedista")
        # data/hora preservadas do rascunho anterior
        self.assertEqual(r1["scheduledDate"], r2["scheduledDate"])

    def test_cancelar_descarta_rascunho(self):
        conn = self._conn()
        asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        env = asyncio.run(agendar_por_voz(conn, self.UID, self.PID, "cancela"))
        self.assertEqual(env["acao"], "AGENDAR_CANCELADO")
        self.assertIsNone(obter_rascunho(self.UID, self.PID))
        conn.fetchrow.assert_not_called()

    def test_confirmar_por_botao_grava(self):
        conn = self._conn()
        asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        draft_id = obter_rascunho(self.UID, self.PID)["id"]
        env = asyncio.run(confirmar_rascunho_por_id(
            conn, self.UID, self.PID, draft_id))
        self.assertEqual(env["acao"], "CONSULTA_AGENDADA")

    def test_botao_com_id_forjado_nao_grava(self):
        conn = self._conn()
        asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID,
            "Agenda uma consulta com o cardiologista quinta-feira as 14h"))
        env = asyncio.run(confirmar_rascunho_por_id(
            conn, self.UID, self.PID, "id-inventado"))
        self.assertEqual(env["acao"], "AGENDAR_FALHA")
        conn.fetchrow.assert_not_called()

    def test_sem_data_nao_grava(self):
        conn = self._conn()
        env = asyncio.run(agendar_por_voz(
            conn, self.UID, self.PID, "agenda uma consulta"))
        self.assertEqual(env["acao"], "AGENDAR_FALHA")
        conn.fetchrow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
