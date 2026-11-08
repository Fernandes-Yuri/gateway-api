"""Contrato informativo: DBHA 2025, seção 3.2.3 / Quadro 3.2.

Fonte: https://doi.org/10.36660/abc.20250624
Não executa LLM nem trata uma medição isolada como diagnóstico.
"""
import unittest
from core.prompt import SYSTEM_PROMPT
from conversation_policy import APP_SYSTEM


class PressureReferencePolicyTest(unittest.TestCase):
    def test_reference_is_versioned_and_keeps_the_measurement_context(self):
        self.assertIn("SBC/SBH/SBN 2025, consultório em adultos", SYSTEM_PROMPT)
        self.assertIn("Normal sistólica <120 E diastólica <80", SYSTEM_PROMPT)
        self.assertIn("Pré-hipertensão sistólica 120-139 OU diastólica 80-89", SYSTEM_PROMPT)
        self.assertNotIn("Normal 120-129/80-84", SYSTEM_PROMPT)
        self.assertNotIn("Ótima <120/80", SYSTEM_PROMPT)
        self.assertIn("nunca afirme que o paciente tem uma patologia", SYSTEM_PROMPT)

    def test_conversation_policy_uses_the_same_reference(self):
        self.assertIn("SBC/SBH/SBN 2025", APP_SYSTEM)
        self.assertNotIn("pressão: Ótima", APP_SYSTEM)
