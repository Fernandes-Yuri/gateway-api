"""Verifica paridade das fixtures de teste entre Go e Python sem dependências externas."""
import base64
import binascii
import json
import os
import unittest


def py_validate_e2ee_public_key(raw_key):
    """Lógica exata de family_api.py:validate_e2ee_public_key."""
    if raw_key is None:
        return None
    cleaned = raw_key.strip()
    if not cleaned:
        return None
    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('Chave pública E2EE inválida.')
    if not (32 <= len(decoded) <= 512):
        raise ValueError('Tamanho de chave pública E2EE inválido.')
    return base64.b64encode(decoded).decode('ascii')


def py_parse_e2ee_envelope(raw_blob, strict=False):
    """Lógica exata de family_api.py:parse_e2ee_envelope."""
    if raw_blob is None:
        if strict:
            raise ValueError('Payload E2EE ausente.')
        return None
    cleaned = raw_blob.strip()
    if not cleaned:
        if strict:
            raise ValueError('Payload E2EE vazio.')
        return None
    if not (cleaned.startswith('{') and cleaned.endswith('}')):
        if strict:
            raise ValueError('Payload E2EE malformado.')
        return None
    try:
        envelope = json.loads(cleaned)
    except (ValueError, TypeError):
        if strict:
            raise ValueError('JSON de payload E2EE inválido.')
        return None
    return json.dumps(envelope, separators=(',', ':'))


class FamilyFixturesParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures_family_chat.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.data = json.load(f)

    def test_send_e2ee_message_fixture_envelope(self):
        body = self.data["fixtures"]["send_e2ee_message"]["request"]["body"]
        envelope_str = body["bodyEncrypted"]
        parsed = py_parse_e2ee_envelope(envelope_str, strict=True)
        self.assertIsNotNone(parsed)
        self.assertIn("ciphertext", parsed)
        self.assertIn("iv", parsed)

    def test_publish_ecdh_public_key_fixture(self):
        body = self.data["fixtures"]["publish_ecdh_public_key"]["request"]["body"]
        key_str = body["publicKey"]
        validated = py_validate_e2ee_public_key(key_str)
        self.assertEqual(validated, key_str)

    def test_accept_invitation_fixture_key(self):
        body = self.data["fixtures"]["accept_invitation"]["request"]["body"]
        key_str = body["caregiverPublicKey"]
        validated = py_validate_e2ee_public_key(key_str)
        self.assertEqual(validated, key_str)


if __name__ == "__main__":
    unittest.main()
