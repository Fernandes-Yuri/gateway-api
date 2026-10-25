"""Testes de regressão da Higiene Crítica do Gateway — Fase A."""
import unittest
from unittest.mock import MagicMock, patch


import config
import orb_router
import server


class HigieneFaseATest(unittest.IsolatedAsyncioTestCase):
    def test_canonical_dataset_name(self):
        """Item 1: dataset canônico deve ser dataset_braga.jsonl."""
        self.assertIn("dataset_braga.jsonl", config.default_dataset_path)


    def test_normalize_aliasing_behavior(self):
        """Item 7: normalize() sem redundância de aliasing."""
        normalized = orb_router.normalize("Ação de Graças & Saúde!")
        self.assertEqual(normalized, "acao de gracas & saude!")

    async def test_clinical_alert_non_blocking_push_isolation(self):
        """Item 4: falha de um push não impede o envio para os demais."""
        devices = {
            "cg-1": {"token": "token-1", "role": "CAREGIVER"},
            "cg-2": {"token": "token-2", "role": "CAREGIVER"},
            "patient": {"token": "token-pt", "role": "USER"},
        }
        called_tokens = []

        def fake_push(token, title, body, data):
            if token == "token-1":
                raise TimeoutError("FCM timeout")
            called_tokens.append(token)
            return True

        with patch.object(server, "load_devices", return_value=devices), \
                patch.object(server, "dispatch_push", side_effect=fake_push):
            pool = MagicMock()
            triggered = await server.dispatch_clinical_alert(
                pool, "patient", systolic=170, diastolic=110
            )
            self.assertTrue(triggered)
            self.assertIn("token-2", called_tokens)
            self.assertNotIn("token-pt", called_tokens)


if __name__ == "__main__":
    unittest.main()
