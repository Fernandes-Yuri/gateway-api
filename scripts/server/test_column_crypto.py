"""Suite de testes automatizados para SPEC-12 (Criptografia a Nivel de Aplicacao e Blind Indexing no RDS).

Valida:
  1. Motor criptografico AES-256-GCM (`encrypt_field` / `decrypt_field`) no formato
     `enc:v1:<iv_b64>:<ciphertext_com_tag_b64>` com IV aleatorio de 12 bytes e tag de 16 bytes.
  2. Integridade e deteccao de adulteracao na tag GCM de 16 bytes, no ciphertext,
     no IV de 12 bytes, chave incorreta e payloads malformados.
  3. Retrocompatibilidade defensiva (lazy encryption) para dados legados em texto claro.
  4. Blind Indexing HMAC-SHA256 (`compute_blind_index`) deterministico, truncado em
     32 hex chars, com normalizacao de telefone/texto e busca O(1) em `emergency_contacts`.
  5. Envelope Encryption (AWS KMS + fallback HKDF-SHA256 local/CI sem internet).
  6. Migration `078_application_level_encryption.sql` e integracao transparente nos
     routers/models (`profiles`, `emergency_contacts`, `feedback_replies`, `symptoms_diary`).
"""
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cryptography.exceptions import InvalidTag  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api_identity import firebase_uid  # noqa: E402
from column_crypto import (  # noqa: E402
    BLIND_INDEX_HEX_LENGTH,
    ColumnTamperError,
    ENCRYPTED_PREFIX,
    IV_LENGTH_BYTES,
    KEY_LENGTH_BYTES,
    TAG_LENGTH_BYTES,
    clear_crypto_cache,
    compute_blind_index,
    decrypt_emergency_contact_row,
    decrypt_field,
    derive_key_hkdf,
    encrypt_field,
    encrypt_with_blind_index,
    find_emergency_contacts_by_phone,
    generate_envelope_dek,
    is_encrypted,
    unwrap_dek_with_kms,
    upsert_emergency_contact,
)
from profile_contract import PROFILE_FIELDS, profile_upsert  # noqa: E402
from routers import admin as admin_router  # noqa: E402
from routers import care_os as care_os_router  # noqa: E402
from routers import sync as sync_router  # noqa: E402


class _MockPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class TestAes256GcmColumnCrypto(unittest.TestCase):
    def setUp(self):
        clear_crypto_cache()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DATA_ENCRYPTION_KEY": "spec12-test-master-encryption-key-256bits",
                "BLIND_INDEX_PEPPER": "spec12-test-blind-index-pepper-secret",
                "USE_AWS_KMS_ENVELOPE": "0",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        clear_crypto_cache()

    def test_encrypt_format_iv_and_tag_structure(self):
        """Valida formato enc:v1:<iv_b64>:<ct_com_tag_b64>, IV de 12B e tag de 16B."""
        plaintext = "11988887777"
        encrypted = encrypt_field(plaintext)

        self.assertTrue(encrypted.startswith(ENCRYPTED_PREFIX))
        self.assertTrue(is_encrypted(encrypted))
        self.assertNotIn(plaintext, encrypted)

        parts = encrypted.split(":")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "enc")
        self.assertEqual(parts[1], "v1")

        iv = base64.b64decode(parts[2], validate=True)
        ct_with_tag = base64.b64decode(parts[3], validate=True)

        self.assertEqual(len(iv), IV_LENGTH_BYTES)
        self.assertEqual(
            len(ct_with_tag),
            len(plaintext.encode("utf-8")) + TAG_LENGTH_BYTES,
        )
        self.assertEqual(decrypt_field(encrypted), plaintext)

    def test_csprng_iv_uniqueness_across_encryptions(self):
        """Mesmo plaintext cifrado multiplas vezes gera ciphertexts e IVs distintos."""
        plaintext = "Paciente relatou tontura apos caminhada matinal."
        samples = [encrypt_field(plaintext) for _ in range(20)]
        self.assertEqual(len(set(samples)), 20)

        ivs = {s.split(":")[2] for s in samples}
        self.assertEqual(len(ivs), 20)
        for sample in samples:
            self.assertEqual(decrypt_field(sample), plaintext)

    def test_unicode_clinical_notes_and_edge_values(self):
        """Preserva acentuacao, emojis clinicos, None e string vazia."""
        note = "Cefaleia intensa às 14h — PA 170/105 mmHg, medicação: Losartana 50mg."
        enc = encrypt_field(note)
        self.assertEqual(decrypt_field(enc), note)

        self.assertIsNone(encrypt_field(None))
        self.assertIsNone(decrypt_field(None))
        self.assertEqual(encrypt_field(""), "")
        self.assertEqual(decrypt_field(""), "")

    def test_encrypt_field_is_idempotent_on_already_encrypted_value(self):
        """Nao recifra valores que ja possuem o prefixo enc:v1:."""
        once = encrypt_field("11977776666")
        twice = encrypt_field(once)
        self.assertEqual(once, twice)
        self.assertEqual(decrypt_field(twice), "11977776666")

    def test_legacy_plaintext_fallback_without_prefix(self):
        """Valores legados sem prefixo enc:v1: retornam intactos sem erro (lazy encryption)."""
        legacy_values = [
            "11988887777",
            "(21) 99999-0000",
            "Obrigado pelo seu feedback! Nossa equipe ja analisou.",
            "Dor de cabeca leve no periodo da manha",
        ]
        for legacy in legacy_values:
            with self.subTest(legacy=legacy):
                self.assertFalse(is_encrypted(legacy))
                self.assertEqual(decrypt_field(legacy), legacy)


class TestGcmTagTamperingAndIntegrity(unittest.TestCase):
    def setUp(self):
        clear_crypto_cache()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DATA_ENCRYPTION_KEY": "spec12-integrity-key-alpha",
                "BLIND_INDEX_PEPPER": "spec12-integrity-pepper-alpha",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        clear_crypto_cache()

    def test_tampered_gcm_authentication_tag_raises_error(self):
        """Adulterar 1 bit nos 16 bytes finais (tag GCM) invalida a decifragem."""
        encrypted = encrypt_field("Nota clinica sigilosa: glicemia 280 mg/dL")
        prefix, version, iv_b64, ct_b64 = encrypted.split(":")

        raw = bytearray(base64.b64decode(ct_b64))
        # Inverte 1 bit no ultimo byte (parte da tag de 16 bytes do GCM)
        raw[-1] ^= 0x01
        tampered_ct_b64 = base64.b64encode(bytes(raw)).decode("ascii")
        tampered_payload = f"{prefix}:{version}:{iv_b64}:{tampered_ct_b64}"

        with self.assertRaises(InvalidTag):
            decrypt_field(tampered_payload)
        with self.assertRaises(ValueError):
            decrypt_field(tampered_payload)
        with self.assertRaises(ColumnTamperError):
            decrypt_field(tampered_payload)

    def test_tampered_ciphertext_body_raises_error(self):
        """Adulterar 1 bit no primeiro byte do ciphertext invalida a tag GCM."""
        encrypted = encrypt_field("11988887777")
        prefix, version, iv_b64, ct_b64 = encrypted.split(":")

        raw = bytearray(base64.b64decode(ct_b64))
        raw[0] ^= 0x80
        tampered_ct_b64 = base64.b64encode(bytes(raw)).decode("ascii")
        tampered_payload = f"{prefix}:{version}:{iv_b64}:{tampered_ct_b64}"

        with self.assertRaises(ColumnTamperError):
            decrypt_field(tampered_payload)

    def test_tampered_iv_raises_error(self):
        """Adulterar 1 bit no IV de 12 bytes invalida a autenticacao GCM."""
        encrypted = encrypt_field("11988887777")
        prefix, version, iv_b64, ct_b64 = encrypted.split(":")

        raw_iv = bytearray(base64.b64decode(iv_b64))
        raw_iv[0] ^= 0x01
        tampered_iv_b64 = base64.b64encode(bytes(raw_iv)).decode("ascii")
        tampered_payload = f"{prefix}:{version}:{tampered_iv_b64}:{ct_b64}"

        with self.assertRaises(ColumnTamperError):
            decrypt_field(tampered_payload)

    def test_wrong_encryption_key_rejects_decryption(self):
        """Decifrar com chave diferente falha na verificacao da tag GCM."""
        encrypted = encrypt_field("Dado sensivel protegido")
        with patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "outra-chave-invalida"}):
            with self.assertRaises(ColumnTamperError):
                decrypt_field(encrypted)

    def test_malformed_enc_v1_payloads_rejected(self):
        """Payloads com prefixo enc:v1: mas estrutura corrompida levantam erro."""
        short_iv = base64.b64encode(b"short").decode("ascii")
        valid_iv = base64.b64encode(b"\x00" * 12).decode("ascii")
        short_tag = base64.b64encode(b"too_short").decode("ascii")

        malformed_cases = [
            "enc:v1:apenas_um_bloco",
            "enc:v1:a:b:c",
            "enc:v1::",
            "enc:v1:@@@not_base64@@@:@@@not_base64@@@",
            f"enc:v1:{short_iv}:{short_tag}",
            f"enc:v1:{valid_iv}:{short_tag}",
        ]
        for bad in malformed_cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ColumnTamperError):
                    decrypt_field(bad)


class TestBlindIndexingAndEnvelopeEncryption(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_crypto_cache()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DATA_ENCRYPTION_KEY": "spec12-dek-master-secret",
                "BLIND_INDEX_PEPPER": "spec12-blind-index-pepper",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        clear_crypto_cache()

    def test_blind_index_determinism_length_and_normalization(self):
        """Blind index HMAC-SHA256 tem 32 hex chars e normaliza telefones equivalentes."""
        idx1 = compute_blind_index("11988887777")
        idx2 = compute_blind_index("(11) 98888-7777")
        idx3 = compute_blind_index("  11 98888-7777  ")

        self.assertEqual(len(idx1), BLIND_INDEX_HEX_LENGTH)
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{32}", idx1))
        self.assertEqual(idx1, idx2)
        self.assertEqual(idx1, idx3)

        # Telefone distinto gera indice distinto
        idx_other = compute_blind_index("11988887778")
        self.assertNotEqual(idx1, idx_other)

        # Se passar valor ja cifrado, calcula o mesmo blind index do texto plano
        encrypted_phone = encrypt_field("(11) 98888-7777")
        self.assertEqual(compute_blind_index(encrypted_phone), idx1)

    def test_blind_index_changes_with_pepper_secret(self):
        """Mudar o BLIND_INDEX_PEPPER altera completamente o indice gerado."""
        idx_a = compute_blind_index("11988887777")
        with patch.dict(os.environ, {"BLIND_INDEX_PEPPER": "outro-pepper-secreto"}):
            idx_b = compute_blind_index("11988887777")
        self.assertNotEqual(idx_a, idx_b)

    def test_hkdf_derivation_and_kms_envelope_unwrap(self):
        """Testa derivacao HKDF-SHA256 de 32B e desencapsulamento de DEK via KMS mock."""
        derived = derive_key_hkdf("chave-ambiente-local")
        self.assertEqual(len(derived), KEY_LENGTH_BYTES)
        self.assertEqual(derived, derive_key_hkdf("chave-ambiente-local"))

        raw_dek = os.urandom(32)
        fake_cipher_blob = b"kms-ciphertext-blob-spec12"
        mock_kms = MagicMock()
        mock_kms.generate_data_key.return_value = {
            "Plaintext": raw_dek,
            "CiphertextBlob": fake_cipher_blob,
        }
        mock_kms.decrypt.return_value = {"Plaintext": raw_dek}

        plain_dek, enc_dek_b64 = generate_envelope_dek(
            "arn:aws:kms:us-east-2:123456789012:key/test-key",
            kms_client=mock_kms,
        )
        self.assertEqual(plain_dek, raw_dek)

        clear_crypto_cache()
        unwrapped = unwrap_dek_with_kms(enc_dek_b64, kms_client=mock_kms)
        self.assertEqual(unwrapped, raw_dek)
        mock_kms.decrypt.assert_called_once()

    async def test_emergency_contacts_blind_index_o1_lookup_and_upsert(self):
        """Valida INSERT cifrado e SELECT O(1) via phone_bindex em emergency_contacts."""
        conn = MagicMock()
        stored_enc, stored_bindex = encrypt_with_blind_index("(11) 98888-7777")

        conn.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "user_id": "patient-01",
                "name": "Maria",
                "relation": "Filha",
                "phone": None,
                "phone_encrypted": stored_enc,
                "phone_bindex": stored_bindex,
                "is_primary": True,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        saved = await upsert_emergency_contact(
            conn,
            user_id="patient-01",
            phone="(11) 98888-7777",
            name="Maria",
            relation="Filha",
        )
        self.assertEqual(saved["phone"], "(11) 98888-7777")
        self.assertTrue(is_encrypted(saved["phone_encrypted"]))
        self.assertEqual(saved["phone_bindex"], stored_bindex)

        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "user_id": "patient-01",
                    "name": "Maria",
                    "relation": "Filha",
                    "phone": None,
                    "phone_encrypted": stored_enc,
                    "phone_bindex": stored_bindex,
                }
            ]
        )
        found = await find_emergency_contacts_by_phone(conn, "11988887777")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["phone"], "(11) 98888-7777")

        sql_executed, param_bindex = conn.fetch.call_args.args
        self.assertEqual(
            sql_executed,
            "SELECT * FROM emergency_contacts WHERE phone_bindex = $1",
        )
        self.assertEqual(param_bindex, stored_bindex)

        # Fallback legado na leitura de emergency_contacts
        legacy_row = decrypt_emergency_contact_row(
            {"user_id": "patient-legacy", "phone": "11911112222", "phone_encrypted": None}
        )
        self.assertEqual(legacy_row["phone"], "11911112222")


class TestMigration078AndRoutersIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_crypto_cache()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DATA_ENCRYPTION_KEY": "spec12-router-integration-key",
                "BLIND_INDEX_PEPPER": "spec12-router-integration-pepper",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        clear_crypto_cache()

    def test_migration_078_sql_exists_and_declares_encrypted_columns_and_bindex(self):
        """Verifica que migrations/078_application_level_encryption.sql contem o DDL esperado."""
        repo_root = Path(__file__).resolve().parents[2]
        mig_path = repo_root / "migrations" / "078_application_level_encryption.sql"
        self.assertTrue(mig_path.is_file(), "Migration 078 deve existir em migrations/")

        sql = mig_path.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS emergency_contacts", sql)
        self.assertIn("phone_encrypted TEXT", sql)
        self.assertIn("phone_bindex VARCHAR(64)", sql)
        self.assertIn("idx_emergency_contacts_phone_bindex", sql)
        self.assertIn("ALTER TABLE profiles ALTER COLUMN emergency_contact_phone TYPE TEXT", sql)
        self.assertIn("ALTER TABLE feedback_replies ALTER COLUMN body TYPE TEXT", sql)
        self.assertIn("ALTER TABLE symptoms_diary ALTER COLUMN symptoms_text TYPE TEXT", sql)
        self.assertIn("078_application_level_encryption", sql)

    async def test_profile_upsert_encrypts_and_get_profile_decrypts_with_legacy_fallback(self):
        """Valida cifragem de emergency_contact_phone no upsert e decifragem no GET /api/profile."""
        dto = sync_router.ProfileSyncDto(
            id="user-spec12",
            fullName="Maria Braga",
            emergencyContactName="Joao",
            emergencyContactPhone="11988887777",
        )
        _, values = profile_upsert(dto)
        keys = list(PROFILE_FIELDS)
        stored_phone = values[keys.index("emergency_contact_phone")]

        self.assertTrue(is_encrypted(stored_phone))
        self.assertNotEqual(stored_phone, "11988887777")
        self.assertEqual(decrypt_field(stored_phone), "11988887777")

        # Leitura de perfil cifrado
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "user-spec12",
                "full_name": "Maria Braga",
                "emergency_contact_phone": stored_phone,
                "phone": encrypt_field("11977776666"),
                "birth_date": None,
                "created_at": datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
            }
        )
        with patch.object(sync_router._state, "db_pool", _MockPool(conn)), patch.object(
            sync_router, "require_family_access", AsyncMock()
        ):
            profile_out = await sync_router.get_profile("user-spec12", uid="user-spec12")
        self.assertEqual(profile_out["emergency_contact_phone"], "11988887777")
        self.assertEqual(profile_out["phone"], "11977776666")

        # Leitura de perfil legado em texto plano
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "user-legacy",
                "full_name": "Antigo",
                "emergency_contact_phone": "11900001111",
                "phone": "11922223333",
                "birth_date": None,
                "created_at": None,
                "updated_at": None,
            }
        )
        with patch.object(sync_router._state, "db_pool", _MockPool(conn)), patch.object(
            sync_router, "require_family_access", AsyncMock()
        ):
            legacy_out = await sync_router.get_profile("user-legacy", uid="user-legacy")
        self.assertEqual(legacy_out["emergency_contact_phone"], "11900001111")
        self.assertEqual(legacy_out["phone"], "11922223333")

    async def test_feedback_replies_encrypted_on_insert_and_decrypted_on_pull(self):
        """Valida que respostas de suporte sao gravadas com enc:v1: e lidas em claro."""
        fid = uuid.uuid4()
        reply_id = uuid.uuid4()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"id": fid, "user_id": "patient-1"},
                {"id": reply_id, "created_at": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)},
            ]
        )
        with patch.object(admin_router._state, "db_pool", _MockPool(conn)):
            res = await admin_router.reply_feedback_admin(
                str(fid),
                admin_router.FeedbackReplyCreate(
                    feedbackId=str(fid),
                    body="Orientacao clinica: mantenha hidratacao e procure seu medico.",
                    author="Equipe Clinica",
                ),
                _auth=True,
            )
        self.assertTrue(res["success"])
        persisted_body = conn.fetchrow.call_args.args[4]
        self.assertTrue(is_encrypted(persisted_body))

        # Agora simula pull pelo SyncWorker (1 cifrado + 1 legado em texto claro)
        conn_pull = MagicMock()
        conn_pull.fetch = AsyncMock(
            return_value=[
                {
                    "id": reply_id,
                    "feedback_id": fid,
                    "author": "Equipe Clinica",
                    "body": persisted_body,
                    "created_at": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc),
                },
                {
                    "id": uuid.uuid4(),
                    "feedback_id": fid,
                    "author": "Suporte Legado",
                    "body": "Mensagem legada sem criptografia.",
                    "created_at": datetime(2026, 10, 30, 15, 5, tzinfo=timezone.utc),
                },
            ]
        )
        conn_pull.execute = AsyncMock()
        with patch.object(sync_router._state, "db_pool", _MockPool(conn_pull)):
            replies = await sync_router.list_feedback_replies(uid="patient-1")

        self.assertEqual(len(replies), 2)
        self.assertEqual(
            replies[0]["body"],
            "Orientacao clinica: mantenha hidratacao e procure seu medico.",
        )
        self.assertEqual(replies[1]["body"], "Mensagem legada sem criptografia.")

    async def test_symptoms_diary_and_rescue_card_transparent_encryption(self):
        """Valida cifragem e decifragem em symptoms_diary e rescue_card."""
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
        app = FastAPI()
        care_os_router.configure_state(
            db_pool=_MockPool(conn),
            public_base_url="https://test.invalid",
        )
        app.include_router(care_os_router.router)
        app.dependency_overrides[firebase_uid] = lambda: "patient-01"
        client = TestClient(app)

        check_in_resp = client.post(
            "/api/symptoms/check-in",
            json={
                "patient_id": "patient-01",
                "reported_at": "2026-10-30T14:00:00Z",
                "symptoms_text": "Palpitacao e falta de ar leve",
                "sleep_quality": 3,
                "disposition": 3,
                "input_method": "TEXT",
            },
        )
        self.assertEqual(check_in_resp.status_code, 201, check_in_resp.text)
        inserted_symptom = conn.fetchrow.call_args.args[4]
        self.assertTrue(is_encrypted(inserted_symptom))
        self.assertEqual(decrypt_field(inserted_symptom), "Palpitacao e falta de ar leve")

        # Leitura via GET /api/patients/{id}/symptoms-diary (1 cifrado + 1 legado)
        conn.fetchrow = AsyncMock(
            return_value={"caregiver_role": "ADMIN_CHILD", "permissions": ["care:read"]}
        )
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "patient_id": "patient-01",
                    "reported_by": "patient-01",
                    "reported_at": datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc),
                    "symptoms_text": inserted_symptom,
                    "sleep_quality": 3,
                    "disposition": 3,
                    "input_method": "TEXT",
                },
                {
                    "id": uuid.uuid4(),
                    "patient_id": "patient-01",
                    "reported_by": "patient-01",
                    "reported_at": datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc),
                    "symptoms_text": "Sintoma legado em texto claro",
                    "sleep_quality": 4,
                    "disposition": 4,
                    "input_method": "VOICE",
                },
            ]
        )
        diary_resp = client.get("/api/patients/patient-01/symptoms-diary")
        self.assertEqual(diary_resp.status_code, 200, diary_resp.text)
        items = diary_resp.json()
        self.assertEqual(items[0]["symptoms_text"], "Palpitacao e falta de ar leve")
        self.assertEqual(items[1]["symptoms_text"], "Sintoma legado em texto claro")


if __name__ == "__main__":
    unittest.main()
