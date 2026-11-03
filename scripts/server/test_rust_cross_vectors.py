"""Testes unitarios de vetores cruzados para paridade Python <-> Rust (SPEC-12 & SPEC-10)."""
import json
import os
import unittest

from column_crypto import (
    decrypt_field,
    compute_blind_index,
    normalize_for_blind_index,
)


class TestRustCrossVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "../rust_migration/fixtures_crypto_vectors.json",
            )
        )
        if not os.path.exists(fixtures_path):
            raise unittest.SkipTest(
                f"Arquivo de fixtures {fixtures_path} nao encontrado."
            )

        with open(fixtures_path, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def test_ale_vectors_decryption(self):
        vectors = self.fixtures.get("ale_vectors", [])
        self.assertGreater(len(vectors), 0, "Nenhum vetor ALE encontrado nas fixtures.")

        for v in vectors:
            pt = v["plaintext"]
            ct = v["ciphertext"]
            decrypted = decrypt_field(ct)
            self.assertEqual(
                decrypted,
                pt,
                f"Decifragem cruzada falhou para '{pt}': obtido '{decrypted}'",
            )

    def test_blind_index_consistency(self):
        vectors = self.fixtures.get("ale_vectors", [])
        for v in vectors:
            pt = v["plaintext"]
            expected_bindex = v["blind_index"]
            expected_normalized = v["normalized"]

            actual_normalized = normalize_for_blind_index(pt)
            self.assertEqual(actual_normalized, expected_normalized)

            actual_bindex = compute_blind_index(pt)
            self.assertEqual(actual_bindex, expected_bindex)


if __name__ == "__main__":
    unittest.main()
