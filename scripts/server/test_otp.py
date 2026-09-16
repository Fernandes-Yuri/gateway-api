"""Testes do plano B OTP (sem template)."""
import unittest

try:
    from whatsapp_service import build_wa_link, is_otp_request, phones_match
except ImportError as _e:
    raise unittest.SkipTest(f"sem deps de servidor: {_e}")


class OtpRequestTests(unittest.TestCase):
    def test_explicit_requests(self):
        for text in ("Quero meu código Braga Saúde", "manda meu código",
                     "meu código", "preciso do codigo", "envia o código"):
            with self.subTest(text=text):
                self.assertTrue(is_otp_request(text), text)

    def test_normal_chat_ignored(self):
        for text in ("bom dia", "pressão 12 por 8", "obrigado pela ajuda",
                     "qual o código do banco?"):
            with self.subTest(text=text):
                self.assertFalse(is_otp_request(text), text)

    def test_too_long_ignored(self):
        self.assertFalse(is_otp_request("quero meu código " + "x" * 60))


class PhoneMatchTests(unittest.TestCase):
    def test_formats(self):
        self.assertTrue(phones_match("(11) 96780-8252", "5511967808252"))
        self.assertTrue(phones_match("11967808252", "5511967808252"))
        self.assertTrue(phones_match("5511967808252", "5511967808252"))
        self.assertFalse(phones_match("11967808252", "5511987654321"))
        self.assertFalse(phones_match("", "5511967808252"))

    def test_wa_link(self):
        link = build_wa_link()
        self.assertTrue(link.startswith("https://wa.me/5511967808252?text="))
        self.assertIn("Quero", link)


if __name__ == "__main__":
    unittest.main()
