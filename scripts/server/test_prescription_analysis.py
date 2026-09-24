"""Testes automatizados da Análise Segura de Receitas Médicas com Árbitro ANVISA."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Garante que scripts/server esteja no sys.path para execução direta via unittest
server_dir = Path(__file__).resolve().parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import care_os
from schemas.medications import PrescriptionAnalysisResponse
from services.prescription_analyzer import (
    _call_gemini_vision,
    extract_raw_ocr_text,
    extract_structured_prescription,
    reconcile_and_verify,
)


class _MockPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _create_test_client(conn, uid: str | None = None):
    app = FastAPI()
    care_os.configure_state(db_pool=_MockPool(conn), public_base_url="https://api.bragasaude.online")
    app.include_router(care_os.router)
    if uid is not None:
        app.dependency_overrides[firebase_uid] = lambda: uid
    return TestClient(app)


class TestPrescriptionArbitrationUnit(unittest.IsolatedAsyncioTestCase):
    """Testes unitários do Motor 3 (Árbitro e Dupla Checagem ANVISA)."""

    async def test_sucesso_concordancia_total(self):
        """Simula Motor 1 e Motor 2 concordando em 'Losartana 50mg' com catálogo ANVISA."""
        structured_items = [
            {
                "name": "Losartana",
                "dosage": "50mg",
                "frequency": "1x ao dia",
                "instructions": "Tomar pela manhã",
            }
        ]
        raw_text = "Receita Médica:\n1. Losartana Potássica 50mg - Tomar 1x ao dia pela manhã."

        db_conn = MagicMock()
        db_conn.fetch = AsyncMock(
            return_value=[
                {
                    "ean_barcode": "7896004715506",
                    "name": "Losartana Potássica 50mg",
                    "concentration": "50 mg",
                    "active_principle": "Losartana Potássica",
                }
            ]
        )

        results = await reconcile_and_verify(structured_items, raw_text, db_conn)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.name, "Losartana")
        self.assertFalse(item.name_divergent)
        self.assertEqual(item.dosage, "50mg")
        self.assertFalse(item.dosage_divergent)
        self.assertEqual(item.dosage_mg, 50.0)
        self.assertEqual(item.frequency, "1x ao dia")
        self.assertFalse(item.frequency_divergent)
        self.assertEqual(item.suggested_times, ["08:00"])
        self.assertEqual(item.ean_barcode, "7896004715506")
        self.assertEqual(item.active_principle, "Losartana Potássica")
        self.assertFalse(item.requires_human_fill)
        self.assertEqual(item.confidence_score, 1.0)
        self.assertIsNone(item.divergence_reason)

    async def test_divergencia_na_dose_texto_bruto(self):
        """Simula Motor 2 retornando '50mg', mas Motor 1 não encontrando o texto correspondente."""
        structured_items = [
            {
                "name": "Losartana",
                "dosage": "50mg",
                "frequency": "1x ao dia",
            }
        ]
        # O texto do OCR bruto só contém 25mg, não 50mg
        raw_text = "Receita Médica:\n1. Losartana Potássica 25mg - Tomar 1x ao dia."

        db_conn = MagicMock()
        db_conn.fetch = AsyncMock(
            return_value=[
                {
                    "ean_barcode": "7896004715506",
                    "name": "Losartana Potássica 50mg",
                    "concentration": "50 mg",
                    "active_principle": "Losartana Potássica",
                }
            ]
        )

        results = await reconcile_and_verify(structured_items, raw_text, db_conn)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.dosage, "")
        self.assertTrue(item.dosage_divergent)
        self.assertTrue(item.requires_human_fill)
        self.assertEqual(item.confidence_score, 0.0)
        self.assertIn("Dosagem não confirmada", item.divergence_reason)

    async def test_sanidade_anvisa_dose_inexistente(self):
        """Simula dose inexistente no mercado brasileiro (ex: Losartana 30mg). Dose DEVE ser anulada."""
        structured_items = [
            {
                "name": "Losartana",
                "dosage": "30mg",
                "frequency": "1x ao dia",
            }
        ]
        # Ambos os motores 1 e 2 leram 30mg
        raw_text = "Receita Médica:\n1. Losartana 30mg - Tomar 1x ao dia."

        db_conn = MagicMock()
        # O catálogo ANVISA só possui Losartana 50mg
        db_conn.fetch = AsyncMock(
            return_value=[
                {
                    "ean_barcode": "7896004715506",
                    "name": "Losartana Potássica 50mg",
                    "concentration": "50 mg",
                    "active_principle": "Losartana Potássica",
                }
            ]
        )

        results = await reconcile_and_verify(structured_items, raw_text, db_conn)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.dosage, "")
        self.assertTrue(item.dosage_divergent)
        self.assertTrue(item.requires_human_fill)
        self.assertEqual(item.confidence_score, 0.0)
        self.assertIn("não existe no catálogo ANVISA", item.divergence_reason)

    async def test_caligrafia_duvidosa_anula_campo_regra_de_ouro(self):
        """Caligrafia duvidosa na dose ou frequência zera os campos e aciona preenchimento humano."""
        structured_items = [
            {
                "name": "Losartana",
                "dosage": "caligrafia incerta entre 25mg e 50mg",
                "frequency": "1x ao dia",
                "caligrafia_duvidosa": True,
            }
        ]
        raw_text = "Receita com rasuras: Losartana [ilegível]"

        db_conn = MagicMock()
        db_conn.fetch = AsyncMock(return_value=[])

        results = await reconcile_and_verify(structured_items, raw_text, db_conn)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.name, "")
        self.assertTrue(item.name_divergent)
        self.assertEqual(item.dosage, "")
        self.assertTrue(item.dosage_divergent)
        self.assertTrue(item.requires_human_fill)
        self.assertEqual(item.confidence_score, 0.0)

    async def test_pdf_ocr_extraction_via_fitz(self):
        """Valida que extract_raw_ocr_text extrai texto diretamente de PDF com PyMuPDF."""
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Losartana Potassica 50mg 1x ao dia")
        pdf_bytes = doc.tobytes()

        extracted_text = await extract_raw_ocr_text(pdf_bytes, "receita.pdf")
        self.assertIn("Losartana Potassica 50mg", extracted_text)


class TestPrescriptionAnalysisEndpoint(unittest.TestCase):
    """Testes de integração do endpoint /api/family/patients/{patient_id}/prescriptions/analyze."""

    def test_sucesso_endpoint_concordancia_total(self):
        """Endpoint retorna 200 e PrescriptionAnalysisResponse quando motores concordam."""
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="Dra. Maria Cuidadora")
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "ean_barcode": "7896004715506",
                    "name": "Losartana Potássica 50mg",
                    "concentration": "50 mg",
                    "active_principle": "Losartana Potássica",
                }
            ]
        )

        client = _create_test_client(conn, uid="patient-001")

        with patch(
            "routers.care_os.extract_raw_ocr_text",
            new_callable=AsyncMock,
            return_value="Losartana Potássica 50mg 1x ao dia",
        ), patch(
            "routers.care_os.extract_structured_prescription",
            new_callable=AsyncMock,
            return_value=[{"name": "Losartana", "dosage": "50mg", "frequency": "1x ao dia"}],
        ):
            response = client.post(
                "/api/family/patients/patient-001/prescriptions/analyze",
                files={"file": ("receita.png", b"fake_png_binary_data", "image/png")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        validated = PrescriptionAnalysisResponse.model_validate(data)
        self.assertEqual(validated.status, "success")
        self.assertIn("/api/files/prescriptions/", validated.prescription_image_url)
        self.assertEqual(len(validated.medications), 1)

        med = validated.medications[0]
        self.assertEqual(med.name, "Losartana")
        self.assertFalse(med.name_divergent)
        self.assertEqual(med.dosage, "50mg")
        self.assertFalse(med.dosage_divergent)
        self.assertFalse(med.requires_human_fill)
        self.assertEqual(med.ean_barcode, "7896004715506")

    def test_divergencia_na_dose_endpoint(self):
        """Endpoint retorna dosage vazia e flags divergent/requires_human_fill se OCR não encontrar a dose."""
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="Dra. Maria Cuidadora")
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(return_value=[])

        client = _create_test_client(conn, uid="patient-001")

        with patch(
            "routers.care_os.extract_raw_ocr_text",
            new_callable=AsyncMock,
            return_value="Texto sem indicacao de dosagem legivel",
        ), patch(
            "routers.care_os.extract_structured_prescription",
            new_callable=AsyncMock,
            return_value=[{"name": "Losartana", "dosage": "50mg", "frequency": "1x ao dia"}],
        ):
            response = client.post(
                "/api/family/patients/patient-001/prescriptions/analyze",
                files={"file": ("receita.jpg", b"fake_jpg_binary_data", "image/jpeg")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        med = data["medications"][0]
        self.assertEqual(med["dosage"], "")
        self.assertTrue(med["dosage_divergent"])
        self.assertTrue(med["requires_human_fill"])

    def test_sanidade_anvisa_dose_inexistente_endpoint(self):
        """Endpoint anula dose incompatível com o catálogo ANVISA."""
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="Dra. Maria Cuidadora")
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        # Catálogo tem apenas 50mg
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "ean_barcode": "7896004715506",
                    "name": "Losartana Potássica 50mg",
                    "concentration": "50 mg",
                    "active_principle": "Losartana Potássica",
                }
            ]
        )

        client = _create_test_client(conn, uid="patient-001")

        with patch(
            "routers.care_os.extract_raw_ocr_text",
            new_callable=AsyncMock,
            return_value="Losartana 30mg 1x ao dia",
        ), patch(
            "routers.care_os.extract_structured_prescription",
            new_callable=AsyncMock,
            return_value=[{"name": "Losartana", "dosage": "30mg", "frequency": "1x ao dia"}],
        ):
            response = client.post(
                "/api/family/patients/patient-001/prescriptions/analyze",
                files={"file": ("receita.pdf", b"%PDF-fake-header", "application/pdf")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        med = data["medications"][0]
        self.assertEqual(med["dosage"], "")
        self.assertTrue(med["dosage_divergent"])
        self.assertTrue(med["requires_human_fill"])

    def test_falta_de_permissao_retorna_403(self):
        """Usuário sem permissão no paciente recebe 403 Forbidden."""
        conn = MagicMock()
        # Sem vínculo ativo ou papel sem permissão medication:write
        conn.fetchrow = AsyncMock(return_value=None)

        client = _create_test_client(conn, uid="intruder_user")
        response = client.post(
            "/api/family/patients/patient-001/prescriptions/analyze",
            files={"file": ("receita.png", b"fake_png", "image/png")},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("não está autorizado", response.json()["detail"])

    def test_auditoria_lgpd_prescricao_analisada(self):
        """Garante inserção de auditoria LGPD em care_audit_trail com action_type = 'PRESCRIPTION_ANALYZED'."""
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="Familiar Responsável")
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(return_value=[])

        client = _create_test_client(conn, uid="patient-001")

        with patch(
            "routers.care_os.extract_raw_ocr_text",
            new_callable=AsyncMock,
            return_value="AAS 100mg",
        ), patch(
            "routers.care_os.extract_structured_prescription",
            new_callable=AsyncMock,
            return_value=[{"name": "AAS", "dosage": "100mg", "frequency": "1x ao dia"}],
        ):
            response = client.post(
                "/api/family/patients/patient-001/prescriptions/analyze",
                files={"file": ("receita_medica.png", b"fake_png_data", "image/png")},
            )

        self.assertEqual(response.status_code, 200)

        # Verifica se care_audit_trail foi chamado
        executed_calls = conn.execute.await_args_list
        audit_call = None
        for call in executed_calls:
            query = call.args[0] if call.args else ""
            if "care_audit_trail" in query and "PRESCRIPTION_ANALYZED" in query:
                audit_call = call
                break

        self.assertIsNotNone(audit_call, "INSERT INTO care_audit_trail com PRESCRIPTION_ANALYZED não foi chamado!")
        details = json.loads(audit_call.args[4])
        self.assertEqual(details["filename"], "receita_medica.png")
        self.assertEqual(details["content_type"], "image/png")
        self.assertEqual(details["medications_count"], 1)

    def test_tipo_de_arquivo_invalido_rejeitado(self):
        """Extensões e MIME types não suportados devem ser rejeitados com 415."""
        conn = MagicMock()
        client = _create_test_client(conn, uid="patient-001")

        response = client.post(
            "/api/family/patients/patient-001/prescriptions/analyze",
            files={"file": ("malware.exe", b"malicious binary", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 415)

    def test_arquivo_vazio_rejeitado(self):
        """Arquivo vazio deve retornar 400 Bad Request."""
        conn = MagicMock()
        client = _create_test_client(conn, uid="patient-001")

        response = client.post(
            "/api/family/patients/patient-001/prescriptions/analyze",
            files={"file": ("vazio.png", b"", "image/png")},
        )
        self.assertEqual(response.status_code, 400)


class TestGeminiVisionPriorityAndFallback(unittest.IsolatedAsyncioTestCase):
    """Testes unitários de prioridade do Gemini 1.5 Flash e fallback resiliente para Groq Vision."""

    async def test_gemini_prioritario_quando_chave_presente(self):
        """Gemini 1.5 Flash deve ser chamado com prioridade quando GEMINI_API_KEY estiver configurada."""
        gemini_ocr_text = "Losartana Potassica 50mg via Gemini"
        gemini_structured_json = json.dumps(
            {
                "medications": [
                    {
                        "name": "Losartana",
                        "dosage": "50mg",
                        "frequency": "1x ao dia",
                    }
                ]
            }
        )

        with patch("services.prescription_analyzer.GEMINI_API_KEY", "test-gemini-key"), patch(
            "services.prescription_analyzer._call_gemini_vision",
            new_callable=AsyncMock,
            side_effect=[gemini_ocr_text, gemini_structured_json],
        ) as mock_gemini, patch(
            "services.prescription_analyzer.httpx.AsyncClient.post"
        ) as mock_groq_http:
            # Motor 1 (OCR Estrito)
            ocr_res = await extract_raw_ocr_text(b"image_bytes", "receita.jpg")
            self.assertEqual(ocr_res, gemini_ocr_text)

            # Motor 2 (Extração Estruturada)
            meds_res = await extract_structured_prescription(b"image_bytes", "image/jpeg")
            self.assertEqual(len(meds_res), 1)
            self.assertEqual(meds_res[0]["name"], "Losartana")
            self.assertEqual(meds_res[0]["dosage"], "50mg")

            # Garante que o Gemini foi chamado para ambos os motores
            self.assertEqual(mock_gemini.await_count, 2)
            # Garante que o Groq NÃO foi acionado (prioridade respeitada)
            mock_groq_http.assert_not_called()

    async def test_fallback_transparente_para_groq_em_falha_do_gemini(self):
        """Se o Gemini retornar erro HTTP, timeout ou None, o serviço deve fazer fallback para o Groq."""
        groq_ocr_resp = MagicMock()
        groq_ocr_resp.status_code = 200
        groq_ocr_resp.json.return_value = {
            "choices": [{"message": {"content": "Losartana 50mg via Groq Fallback"}}]
        }

        groq_struct_resp = MagicMock()
        groq_struct_resp.status_code = 200
        groq_struct_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "medications": [
                                    {
                                        "name": "Losartana",
                                        "dosage": "50mg",
                                        "frequency": "1x ao dia",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

        with patch("services.prescription_analyzer.GEMINI_API_KEY", "test-gemini-key"), patch(
            "services.prescription_analyzer._call_gemini_vision",
            new_callable=AsyncMock,
            return_value=None,  # Simula erro ou timeout do Gemini
        ) as mock_gemini, patch(
            "services.prescription_analyzer.GROQ_API_KEY", "test-groq-key"
        ), patch(
            "services.prescription_analyzer.httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=[groq_ocr_resp, groq_struct_resp],
        ) as mock_groq:
            # Motor 1: Gemini falha -> cai no Groq
            ocr_res = await extract_raw_ocr_text(b"image_bytes", "receita.png")
            self.assertEqual(ocr_res, "Losartana 50mg via Groq Fallback")

            # Motor 2: Gemini falha -> cai no Groq
            meds_res = await extract_structured_prescription(b"image_bytes", "image/png")
            self.assertEqual(len(meds_res), 1)
            self.assertEqual(meds_res[0]["name"], "Losartana")

            # Gemini foi tentado primeiro
            self.assertEqual(mock_gemini.await_count, 2)
            # Groq foi acionado como fallback transparente
            self.assertEqual(mock_groq.await_count, 2)

    async def test_operacao_normal_apenas_com_groq_quando_gemini_key_none(self):
        """Quando GEMINI_API_KEY for None, Gemini não deve ser chamado e Groq deve operar diretamente."""
        groq_resp = MagicMock()
        groq_resp.status_code = 200
        groq_resp.json.return_value = {
            "choices": [{"message": {"content": "Losartana 50mg direto via Groq"}}]
        }

        with patch("services.prescription_analyzer.GEMINI_API_KEY", None), patch(
            "services.prescription_analyzer._call_gemini_vision",
            new_callable=AsyncMock,
        ) as mock_gemini, patch(
            "services.prescription_analyzer.GROQ_API_KEY", "test-groq-key"
        ), patch(
            "services.prescription_analyzer.httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=groq_resp,
        ) as mock_groq:
            res = await extract_raw_ocr_text(b"image_bytes", "receita.jpg")
            self.assertEqual(res, "Losartana 50mg direto via Groq")
            mock_gemini.assert_not_called()
            self.assertEqual(mock_groq.await_count, 1)

    async def test_call_gemini_vision_rest_sucesso(self):
        """Valida que _call_gemini_vision formata o payload e faz o parse correto da resposta do Google AI Studio."""
        fake_gemini_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Receita transcrita com sucesso pelo Gemini 1.5 Flash"}
                        ]
                    }
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_gemini_response

        with patch("services.prescription_analyzer.GEMINI_API_KEY", "fake-api-key"), patch(
            "services.prescription_analyzer.httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ) as mock_http:
            result = await _call_gemini_vision(b"fake_bytes", "prompt de teste", "image/png")
            self.assertEqual(result, "Receita transcrita com sucesso pelo Gemini 1.5 Flash")

            # Verifica URL com query param key
            called_url = mock_http.await_args[0][0]
            self.assertIn("key=fake-api-key", called_url)
            self.assertIn("gemini-1.5-flash:generateContent", called_url)

    async def test_call_gemini_vision_rest_erro_retorna_none(self):
        """Valida que falhas HTTP 500 ou exceções no Gemini retornam None sem quebrar o fluxo."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("services.prescription_analyzer.GEMINI_API_KEY", "fake-api-key"), patch(
            "services.prescription_analyzer.httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await _call_gemini_vision(b"fake_bytes", "prompt de teste")
            self.assertIsNone(result)

        # Simula exceção de rede
        with patch("services.prescription_analyzer.GEMINI_API_KEY", "fake-api-key"), patch(
            "services.prescription_analyzer.httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=Exception("Connection timeout"),
        ):
            result = await _call_gemini_vision(b"fake_bytes", "prompt de teste")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

