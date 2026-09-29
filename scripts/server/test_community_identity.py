import unittest
from community_identity import normalize_nickname


class CommunityIdentityTests(unittest.TestCase):
    def test_acentos_e_espacos(self):
        self.assertEqual(normalize_nickname("  João  F. "), "João F.")

    def test_remocao(self):
        self.assertEqual(normalize_nickname("  "), "")

    def test_rejeita_markup_controles_e_limites(self):
        for value in ("<script>", "ab\ncd", "a", "a" * 25, "...", "ab\u202ecd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_nickname(value)
