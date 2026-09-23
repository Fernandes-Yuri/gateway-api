"""Testes automatizados do Catálogo de Medicamentos ANVISA / Farmácia Popular."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import care_os
from schemas.medications import BarcodeMedication

# Registro representativo para mock nos testes de rota
MOCK_MEDICATION = {
    "ean_barcode": "7896004715506",
    "name": "Losartana Potássica 50mg",
    "active_principle": "Losartana Potássica",
    "concentration": "50 mg",
    "pharmaceutical_form": "Comprimido revestido",
    "manufacturer": "EMS",
    "farmacia_popular_eligible": True,
    "source_name": "Farmacia Popular / ANVISA",
    "source_url": "https://www.gov.br/saude/pt-br/composicao/sectics/farmacia-popular",
    "source_checked_at": datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc),
}


def is_valid_ean13(ean: str) -> bool:
    """Valida se uma string é um EAN-13 numérico válido conforme o algoritmo GS1 (módulo 10)."""
    if not isinstance(ean, str) or len(ean) != 13 or not ean.isdigit():
        return False
    weighted_sum = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(ean[:12]))
    expected_check = (10 - (weighted_sum % 10)) % 10
    return int(ean[12]) == expected_check


class _MockPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _create_test_client(conn, uid: str | None = None):
    app = FastAPI()
    care_os.configure_state(db_pool=_MockPool(conn), public_base_url="https://test.invalid")
    app.include_router(care_os.router)
    if uid is not None:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app)


class AnvisaCatalogEndpointTests(unittest.TestCase):
    def test_get_existing_medication_by_barcode_returns_200_and_valid_schema(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=dict(MOCK_MEDICATION))

        client = _create_test_client(conn, uid="caregiver-01")
        response = client.get(f"/api/anvisa/medications/barcode/{MOCK_MEDICATION['ean_barcode']}")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["ean_barcode"], MOCK_MEDICATION["ean_barcode"])
        self.assertEqual(data["name"], MOCK_MEDICATION["name"])
        self.assertEqual(data["manufacturer"], MOCK_MEDICATION["manufacturer"])
        self.assertTrue(data["farmacia_popular_eligible"])

        # Validação de conformidade com o schema Pydantic BarcodeMedication
        validated = BarcodeMedication.model_validate(data)
        self.assertEqual(validated.ean_barcode, MOCK_MEDICATION["ean_barcode"])

    def test_get_nonexistent_barcode_returns_404_with_prescriptions_guidance(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)

        client = _create_test_client(conn, uid="caregiver-01")
        response = client.get("/api/anvisa/medications/barcode/7899999999993")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"],
            "EAN não encontrado no catálogo versionado. Cadastre manualmente e confira a receita.",
        )

    def test_get_barcode_invalid_length_returns_422(self):
        conn = MagicMock()
        client = _create_test_client(conn, uid="caregiver-01")

        for invalid_ean in ["123", "789600471550", "78960047155060"]:
            with self.subTest(invalid_ean=invalid_ean):
                response = client.get(f"/api/anvisa/medications/barcode/{invalid_ean}")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "EAN-13 inválido.")

    def test_get_barcode_with_alphabetic_characters_returns_422(self):
        conn = MagicMock()
        client = _create_test_client(conn, uid="caregiver-01")

        for invalid_ean in ["789600471550A", "ABCDEFGHIJKLM", "789600-715506"]:
            with self.subTest(invalid_ean=invalid_ean):
                response = client.get(f"/api/anvisa/medications/barcode/{invalid_ean}")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "EAN-13 inválido.")

    def test_get_barcode_unauthenticated_returns_401(self):
        conn = MagicMock()
        client = _create_test_client(conn, uid=None)
        response = client.get(f"/api/anvisa/medications/barcode/{MOCK_MEDICATION['ean_barcode']}")
        self.assertEqual(response.status_code, 401)


class MigrationSqlIntegrityTests(unittest.TestCase):
    def test_migration_019_contains_50_valid_ean13_medications(self):
        """Valida diretamente o arquivo SQL da Migration 019 como fonte única da verdade."""
        migration_file = Path(__file__).resolve().parent.parent.parent / "migrations" / "019_seed_anvisa_top50_medications.sql"
        self.assertTrue(migration_file.exists(), "Arquivo da migration 019 não encontrado.")

        sql_content = migration_file.read_text(encoding="utf-8")

        # Extrai os EANs inseridos no SQL ('789...')
        eans = re.findall(r"\('([0-9]{13})',", sql_content)

        self.assertEqual(len(eans), 50, f"A migration deve conter exatamente 50 medicamentos, encontrados: {len(eans)}")
        self.assertEqual(len(set(eans)), 50, "Todos os 50 EANs na migration devem ser únicos.")

        for ean in eans:
            self.assertTrue(is_valid_ean13(ean), f"EAN {ean} extraído da migration 019 possui checksum inválido.")

    def test_migration_019_has_lock_timeout_and_idempotence(self):
        migration_file = Path(__file__).resolve().parent.parent.parent / "migrations" / "019_seed_anvisa_top50_medications.sql"
        sql_content = migration_file.read_text(encoding="utf-8")

        self.assertIn("SET LOCAL lock_timeout = '10s';", sql_content)
        self.assertIn("ON CONFLICT (ean_barcode) DO UPDATE", sql_content)
        self.assertIn("INSERT INTO schema_migrations", sql_content)
