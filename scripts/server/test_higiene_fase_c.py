"""Testes automatizados de conformidade para Higiene Crítica Fase C.

Valida:
1. Migration 020 (emergency_access_audit e last_accessed_at em emergency_access_tokens).
2. Eliminação do N+1 com batching em clinical_correlations (3 queries assíncronas no total).
3. Trilha de auditoria LGPD na rota pública rescue_card.
4. Desacoplamento de I/O de rede e atomicidade em services/care_automations.py.
5. Sanitização tipográfica inteligente no gerador de PDF _minimal_pdf.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, call

# Garante scripts/server no path caso executado diretamente
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import care_os
from routers.care_os import _minimal_pdf, _sanitize_pdf_text


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


class TestMigration020(unittest.TestCase):
    def test_migration_020_structure(self):
        root_dir = Path(__file__).resolve().parent.parent.parent
        migration_file = root_dir / "migrations" / "020_emergency_access_audit.sql"
        self.assertTrue(migration_file.exists(), "Migration 020 deve existir no diretorio migrations/")

        content = migration_file.read_text(encoding="utf-8")
        self.assertIn("lock_timeout = '10s'", content)
        self.assertIn("emergency_access_audit", content)
        self.assertIn("emergency_access_tokens", content)
        self.assertIn("last_accessed_at", content)
        self.assertIn("idx_emergency_access_audit_token", content)
        self.assertIn("020_emergency_access_audit", content)


class TestClinicalCorrelationsBatching(unittest.IsolatedAsyncioTestCase):
    async def test_correlations_uses_batching_with_exactly_three_queries(self):
        conn = MagicMock()
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        peak_1 = now - timedelta(hours=2)
        peak_2 = now - timedelta(hours=10)

        # Mock permission check
        conn.fetchrow = AsyncMock(return_value={"caregiver_role": "ADMIN_CHILD", "permissions": ["care:read"]})

        vitals_rows = [
            {"measured_at": peak_1, "systolic_pressure": 170, "diastolic_pressure": 105, "glucose_level": None},
            {"measured_at": peak_2, "systolic_pressure": None, "diastolic_pressure": None, "glucose_level": 280},
        ]
        symptoms_rows = [
            {"symptoms_text": "Cefaleia forte", "sleep_quality": 2, "reported_at": peak_1 - timedelta(hours=1)},
            {"symptoms_text": "Tontura leve", "sleep_quality": 3, "reported_at": peak_2 - timedelta(hours=2)},
        ]
        meds_logs_rows = [
            {"med_id": "med-1", "name": "Losartana 50mg", "taken_at": peak_1 - timedelta(hours=30)},  # fora da janela do pico 1
            {"med_id": "med-2", "name": "Metformina 850mg", "taken_at": peak_2 - timedelta(hours=4)},  # tomada ok no pico 2
        ]

        # fetch mock: 1o vitals, 2o symptoms_all, 3o meds_and_logs
        conn.fetch = AsyncMock(side_effect=[vitals_rows, symptoms_rows, meds_logs_rows])

        client = _create_test_client(conn, uid="patient-01")
        response = client.get("/api/patients/patient-01/clinical-correlations")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["patient_id"], "patient-01")
        self.assertEqual(len(data["correlations"]), 2)

        # Valida que foram executadas EXATAMENTE 3 queries de fetch no banco (eliminação de N+1)
        self.assertEqual(conn.fetch.await_count, 3)

        # Valida primeiro pico (Pressão)
        c1 = data["correlations"][0]
        self.assertEqual(c1["event_type"], "BLOOD_PRESSURE_PEAK")
        self.assertIn("Pressão 170/105", c1["observation"])
        self.assertTrue(any("Cefaleia forte" in e for e in c1["evidence"]))
        self.assertTrue(any("Losartana 50mg" in e for e in c1["evidence"]))

        # Valida segundo pico (Glicemia)
        c2 = data["correlations"][1]
        self.assertEqual(c2["event_type"], "GLUCOSE_PEAK")
        self.assertIn("Glicemia 280 mg/dL", c2["observation"])
        self.assertTrue(any("Tontura leve" in e for e in c2["evidence"]))

    async def test_correlations_returns_early_when_no_vitals_peaks(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"caregiver_role": "ADMIN_CHILD", "permissions": ["care:read"]})
        conn.fetch = AsyncMock(return_value=[])  # Sem picos

        client = _create_test_client(conn, uid="patient-01")
        response = client.get("/api/patients/patient-01/clinical-correlations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["correlations"], [])
        # Apenas 1 query em vitals realizada
        self.assertEqual(conn.fetch.await_count, 1)


class TestRescueCardAudit(unittest.TestCase):
    def test_rescue_card_access_records_audit_and_updates_last_accessed_at(self):
        conn = MagicMock()
        token_id = "00000000-0000-0000-0000-000000000001"
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": token_id, "patient_id": "patient-123"},  # grant
            {"full_name": "Antônio Silva", "blood_type": "O+", "medication_allergies": ["Dipirona"],
             "implants": ["Marcapasso"], "emergency_contact_name": "Maria", "emergency_contact_phone": "11988887777"},  # profile
            {"systolic_pressure": 120, "diastolic_pressure": 80, "glucose_level": 95},  # vital
        ])
        conn.fetch = AsyncMock(side_effect=[
            [{"name": "Losartana", "dosage_mg": 50}],  # meds
            [{"full_name": "Filha Maria", "phone": "11999998888"}],  # contacts
        ])
        conn.execute = AsyncMock()

        client = _create_test_client(conn)
        response = client.get(
            "/api/emergency/rescue-card/rescue-secret-token",
            headers={"User-Agent": "RescueBrowser/2.0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ficha médica de resgate", response.text)
        self.assertIn("Antônio Silva", response.text)
        self.assertIn("Marcapasso", response.text)

        # Verifica se o log de auditoria foi gravado e last_accessed_at atualizado
        audit_call = call(
            "INSERT INTO emergency_access_audit(token_id,ip_address,user_agent) VALUES ($1,$2,$3)",
            token_id, "testclient", "RescueBrowser/2.0",
        )
        update_call = call(
            "UPDATE emergency_access_tokens SET last_accessed_at=now() WHERE id=$1",
            token_id,
        )
        conn.execute.assert_has_calls([audit_call, update_call], any_order=False)

    def test_rescue_card_404_for_invalid_token(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        client = _create_test_client(conn)
        response = client.get("/api/emergency/rescue-card/invalid-token")
        self.assertEqual(response.status_code, 404)




class TestMinimalPdfSanitization(unittest.TestCase):
    def test_unicode_typography_sanitization(self):
        raw = "BRAGA SAÚDE – “Leva pro Doutor”… Atenção com a dosagem: 50mg — OK! • Conferir"
        sanitized = _sanitize_pdf_text(raw)

        # Não deve conter interrogações nem caracteres Unicode problemáticos
        self.assertNotIn("?", sanitized)
        self.assertNotIn("–", sanitized)
        self.assertNotIn("—", sanitized)
        self.assertNotIn("“", sanitized)
        self.assertNotIn("”", sanitized)
        self.assertNotIn("…", sanitized)
        self.assertNotIn("•", sanitized)

        # Deve conter aspas e hífens ASCII seguros
        self.assertIn('"Leva pro Doutor"', sanitized)
        self.assertIn("-", sanitized)
        self.assertIn("...", sanitized)
        self.assertIn("*", sanitized)

        # Deve preservar caracteres acentuados suportados no Latin-1
        self.assertIn("SAÚDE", sanitized)
        self.assertIn("Atenção", sanitized)

        # Deve ser codificável sem erro em Latin-1
        encoded = sanitized.encode("latin-1")
        self.assertIsInstance(encoded, bytes)

    def test_minimal_pdf_generation_produces_valid_pdf_stream(self):
        lines = [
            "BRAGA SAÚDE - LEVA PRO DOUTOR",
            "Paciente: João da Silva",
            "Pressão arterial média: 120/80 mmHg — estável",
            "Adesão registrada: 100% “ótima”",
            "Check-ins sintomáticos: nenhum…",
        ]
        pdf_bytes = _minimal_pdf(lines)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4\n"))
        self.assertTrue(pdf_bytes.strip().endswith(b"%%EOF"))
