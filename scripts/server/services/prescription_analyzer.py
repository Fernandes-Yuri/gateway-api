"""Serviço de Análise Segura de Receitas Médicas com Dupla Checagem e Árbitro ANVISA.

Princípio Clínico Inegociável:
Na geriatria, alucinação de IA ou erro de OCR em dosagem (ex: confundir 5mg com 50mg)
é um risco fatal. O serviço implementa dois motores independentes e um árbitro:
1. Motor 1 (OCR Estrito): Extrai o texto literal bruto da receita (caractere a caractere).
2. Motor 2 (Visão Multimodal): Interpreta a imagem e extrai estruturadamente medicamentos,
   doses e horários.
3. Motor 3 (Árbitro & Dupla Checagem contra Catálogo ANVISA):
   - Confronta se a dose e o nome do Motor 2 constam de forma idêntica no texto bruto do Motor 1.
   - Valida se a dosagem existe no mercado brasileiro consultando anvisa_medication_catalog.
   - REGRA DE OURO: Se houver QUALQUER divergência ou caligrafia duvidosa na dose, nome ou
     frequência, o campo retornado DEVE vir OBRIGATORIAMENTE VAZIO (""), com divergent: true
     e requires_human_fill: true. O backend NUNCA chuta um valor.
"""
import base64
import inspect
import json
import logging
import math
import re
import unicodedata
from typing import Optional

import httpx

from config import (
    GEMINI_API_KEY,
    GEMINI_API_URL,
    GEMINI_MODEL_VISION,
    GROQ_API_KEY,
    GROQ_API_URL,
    GROQ_MODEL_VISION,
)
from schemas.medications import AnalyzedMedicationItem

logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """Normaliza texto removendo acentos e convertendo para minúsculas."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def parse_dosage_mg(dosage_str: str) -> Optional[float]:
    """Converte dosagem expressa em string para float em miligramas (mg) quando aplicável."""
    if not dosage_str:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mg|g|mcg|ml|ui)?", dosage_str, re.IGNORECASE)
    if not match:
        return None
    val = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "mg").lower()
    if unit == "mg":
        return val
    if unit == "g":
        return val * 1000.0
    if unit == "mcg":
        return val / 1000.0
    return val


def infer_suggested_times(frequency: str) -> list[str]:
    """Infere horários sugeridos baseados na descrição de posologia/frequência."""
    f = normalize_text(frequency)
    if not f:
        return []
    if any(k in f for k in ["6 em 6", "6/6", "4x", "4 vezes", "4 comprimidos"]):
        return ["06:00", "12:00", "18:00", "00:00"]
    if any(k in f for k in ["8 em 8", "8/8", "3x", "3 vezes", "3 comprimidos"]):
        return ["08:00", "14:00", "20:00"]
    if any(k in f for k in ["12 em 12", "12/12", "2x", "2 vezes", "2 comprimidos"]):
        return ["08:00", "20:00"]
    if any(k in f for k in ["noite", "ao deitar", "dormir", "noturno"]):
        return ["21:00"]
    if any(k in f for k in ["1x", "1 vez", "dia", "manha", "manhã", "24h", "24/24"]):
        return ["08:00"]
    return ["08:00"]


def is_dosage_in_raw_text(dosage_str: str, raw_text: str) -> bool:
    """Verifica se a dosagem consta de forma explícita e não ambígua no texto bruto."""
    if not dosage_str or not raw_text:
        return False
    norm_dose = normalize_text(dosage_str)
    norm_raw = normalize_text(raw_text)

    # Rejeita explicitamente notas de caligrafia incerta ou ambiguidade
    if any(w in norm_dose for w in ["incert", "duvid", "ambig", "entre", " ou ", "/"]):
        return False

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mg|g|mcg|ml|ui)?", dosage_str, re.IGNORECASE)
    if not match:
        return norm_dose in norm_raw

    num_str = match.group(1).replace(",", ".")
    unit_str = (match.group(2) or "mg").lower()
    num_pat = re.escape(num_str).replace(r"\.", r"[.,]")

    # Busca com unidade (ex: 50mg ou 50 mg) garantindo limites estritos de dígito
    pattern_with_unit = rf"(?<![\d.,]){num_pat}\s*{re.escape(unit_str)}(?![\w])"
    if re.search(pattern_with_unit, norm_raw):
        return True

    # Se a dosagem informada não continha unidade explícita, aceita apenas o número isolado
    if not match.group(2):
        pattern_num_only = rf"(?<![\d.,]){num_pat}(?![\d.,\w])"
        if re.search(pattern_num_only, norm_raw):
            return True

    return False


def is_name_in_raw_text(med_name: str, raw_text: str) -> bool:
    """Verifica se o nome do medicamento consta no texto bruto do OCR."""
    if not med_name or not raw_text:
        return False
    norm_raw = normalize_text(raw_text)
    norm_name = normalize_text(med_name)

    if any(w in norm_name for w in ["incert", "duvid", "ambig", "ilegiv"]):
        return False

    clean = re.sub(r"\d+(\.\d+)?\s*(mg|g|mcg|ml|ui|%)?", "", norm_name)
    clean = re.sub(r"(comprimido|comprimidos|gotas|capsula|capsulas|solucao|xarope)", "", clean)
    words = [w for w in re.split(r"[^\w]+", clean) if len(w) >= 3]
    if not words:
        return norm_name in norm_raw
    return any(w in norm_raw for w in words)


def _parse_json_medications(content: str) -> list[dict]:
    content = content.strip()
    if "```" in content:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
        if match:
            content = match.group(1).strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "medications" in parsed:
            return parsed["medications"]
        if isinstance(parsed, list):
            return parsed
    except Exception as exc:
        logger.warning("[prescription_analyzer] Erro de parse JSON: %s. Raw: %s", exc, content[:200])
    return []


async def _call_gemini_vision(image_bytes: bytes, prompt: str, content_type: str = "image/jpeg") -> Optional[str]:
    """Executa inferência de visão multimodal no Google Gemini 1.5 Flash via REST."""
    if not GEMINI_API_KEY:
        return None
    b64_data = base64.b64encode(image_bytes).decode("utf-8")
    mime = content_type if content_type in ["image/jpeg", "image/png", "image/webp"] else "image/jpeg"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": b64_data,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
        },
    }
    url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates and "content" in candidates[0]:
                    parts = candidates[0]["content"].get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip()
            logger.warning(
                "[gemini_vision] Falha HTTP %s: %s (modelo: %s)",
                resp.status_code,
                resp.text,
                GEMINI_MODEL_VISION,
            )
    except Exception as exc:
        logger.warning("[gemini_vision] Exceção ao chamar Gemini (%s): %s", GEMINI_MODEL_VISION, exc)
    return None


async def _call_vision_ocr(image_bytes: bytes) -> str:
    """Motor 1 OCR: transcrição literal estrita caractere a caractere."""
    prompt = (
        "Você é um motor de OCR médico estrito de altíssima precisão clínica. "
        "Sua ÚNICA função é transcrever literalmente caractere a caractere todo o texto legível nesta receita médica. "
        "Regras inegociáveis:\n"
        "1. NÃO interprete, NÃO deduza e NÃO complete nomes ou dosagens ausentes.\n"
        "2. Transcreva exatamente os nomes, números e unidades como escritos na receita.\n"
        "3. Se uma palavra ou número estiver ilegível, rasurado ou borrado, marque exatamente como [ILEGÍVEL].\n"
        "4. Retorne apenas o texto literal transcrito, sem introduções ou comentários."
    )

    # Tenta Gemini 1.5 Flash primeiro (melhor caligrafia em português)
    if GEMINI_API_KEY:
        gemini_res = await _call_gemini_vision(image_bytes, prompt, "image/jpeg")
        if gemini_res:
            return gemini_res

    # Fallback para Groq Vision se Gemini ausente ou falhar
    if not GROQ_API_KEY:
        logger.warning("[_call_vision_ocr] GROQ_API_KEY ausente.")
        return ""

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": GROQ_MODEL_VISION,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GROQ_API_URL, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            logger.warning("[_call_vision_ocr] Falha HTTP %s: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("[_call_vision_ocr] Exceção ao chamar visão: %s", exc)
    return ""


async def extract_raw_ocr_text(image_bytes: bytes, filename: str) -> str:
    """Extrai texto literal bruto da receita (Motor 1 - OCR Estrito).

    Se for PDF: extrai texto direto via PyMuPDF (fitz).
    Se for imagem: faz transcrição literal estrita caractere a caractere.
    """
    is_pdf = filename.lower().endswith(".pdf") or image_bytes.startswith(b"%PDF")
    if is_pdf:
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
            doc = fitz.open(stream=image_bytes, filetype="pdf")
            pages_text = [page.get_text() for page in doc]
            extracted = "\n".join(pages_text).strip()
            if extracted:
                return extracted
            # PDF escaneado (sem camada textual): renderiza primeira página para imagem
            if len(doc) > 0:
                pix = doc[0].get_pixmap()
                image_bytes = pix.tobytes("png")
        except Exception as exc:
            logger.warning("[extract_raw_ocr_text] Erro ao extrair texto de PDF via PyMuPDF: %s", exc)

    return await _call_vision_ocr(image_bytes)


async def extract_structured_prescription(image_bytes: bytes, content_type: str = "image/jpeg") -> list[dict]:
    """Interpreta a imagem e extrai estruturadamente medicamentos, doses e horários (Motor 2)."""
    if content_type == "application/pdf" or image_bytes.startswith(b"%PDF"):
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
            doc = fitz.open(stream=image_bytes, filetype="pdf")
            if len(doc) > 0:
                pix = doc[0].get_pixmap()
                image_bytes = pix.tobytes("png")
                content_type = "image/png"
        except Exception as exc:
            logger.warning("[extract_structured_prescription] Falha ao renderizar PDF para visão: %s", exc)

    prompt = (
        "Você é um assistente clínico de análise segura de receitas médicas geriátricas. "
        "Analise a imagem da receita e extraia cada medicamento prescrito de forma estruturada.\n"
        "Princípio clínico inegociável: Se a caligrafia estiver ilegível, incerta ou ambígua em qualquer campo "
        "(dose, nome ou frequência), informe a dúvida de forma explícita ou deixe o campo em branco.\n\n"
        "Retorne EXCLUSIVAMENTE um objeto JSON no formato:\n"
        "{\n"
        '  "medications": [\n'
        "    {\n"
        '      "name": "Nome do medicamento",\n'
        '      "dosage": "Dose (ex: 50mg, 10mg)",\n'
        '      "frequency": "Posologia/frequência (ex: 1x ao dia, de 12 em 12 horas)",\n'
        '      "instructions": "Instruções adicionais se houver",\n'
        '      "suggested_times": ["08:00"],\n'
        '      "confidence": 0.95,\n'
        '      "caligrafia_duvidosa": false\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    # Tenta Gemini 1.5 Flash primeiro
    if GEMINI_API_KEY:
        gemini_res = await _call_gemini_vision(image_bytes, prompt, content_type)
        if gemini_res:
            meds = _parse_json_medications(gemini_res)
            if meds:
                return meds

    # Fallback para Groq Vision se Gemini ausente ou falhar
    if not GROQ_API_KEY:
        logger.warning("[extract_structured_prescription] GROQ_API_KEY ausente.")
        return []

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": GROQ_MODEL_VISION,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{content_type};base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GROQ_API_URL, json=payload, headers=headers)
            if resp.status_code == 200:
                raw_content = resp.json()["choices"][0]["message"]["content"]
                return _parse_json_medications(raw_content)
            logger.warning("[extract_structured_prescription] Falha HTTP %s: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("[extract_structured_prescription] Exceção ao chamar visão: %s", exc)
    return []



async def reconcile_and_verify(
    structured_items: list[dict],
    raw_text: str,
    db_conn,
) -> list[AnalyzedMedicationItem]:
    """Árbitro e Dupla Checagem contra Texto Bruto e Catálogo ANVISA (Motor 3).

    Garante o princípio clínico de tolerância zero com ambiguidades.
    """
    results: list[AnalyzedMedicationItem] = []

    for item in structured_items:
        raw_name = (item.get("name") or "").strip()
        raw_dosage = (item.get("dosage") or "").strip()
        raw_freq = (item.get("frequency") or "").strip()
        raw_suggested = item.get("suggested_times") or []
        caligrafia_duvidosa = bool(item.get("caligrafia_duvidosa"))

        name_divergent = False
        dosage_divergent = False
        frequency_divergent = False
        requires_human_fill = False
        reasons: list[str] = []

        # -------------------------------------------------------------
        # 1. Verificação do Nome
        # -------------------------------------------------------------
        if not raw_name or caligrafia_duvidosa or not is_name_in_raw_text(raw_name, raw_text):
            name_divergent = True
            final_name = ""
            requires_human_fill = True
            reasons.append("Nome do medicamento não confirmado no texto bruto do OCR ou caligrafia duvidosa.")
        else:
            final_name = raw_name

        # -------------------------------------------------------------
        # 2. Verificação 1: Dosagem no Texto Bruto
        # -------------------------------------------------------------
        if not raw_dosage or caligrafia_duvidosa or not is_dosage_in_raw_text(raw_dosage, raw_text):
            dosage_divergent = True
            reasons.append("Dosagem não confirmada no texto bruto da receita (OCR Estrito).")
        else:
            dosage_divergent = False

        # -------------------------------------------------------------
        # 3. Verificação da Frequência / Posologia
        # -------------------------------------------------------------
        if not raw_freq or caligrafia_duvidosa or any(w in normalize_text(raw_freq) for w in ["incert", "duvid", "ambig"]):
            frequency_divergent = True
            final_freq = ""
            final_suggested_times = []
            requires_human_fill = True
            reasons.append("Frequência/posologia ausente ou duvidosa.")
        else:
            frequency_divergent = False
            final_freq = raw_freq
            final_suggested_times = raw_suggested if raw_suggested else infer_suggested_times(raw_freq)

        # -------------------------------------------------------------
        # 4. Verificação 2: Catálogo ANVISA
        # -------------------------------------------------------------
        ean_barcode: Optional[str] = None
        active_principle: Optional[str] = None
        dosage_mg: Optional[float] = parse_dosage_mg(raw_dosage)

        # Busca por aproximação no catálogo ANVISA
        clean_drug_name = re.sub(r"\d+(\.\d+)?\s*(mg|g|mcg|ml|ui|%)?", "", raw_name, flags=re.IGNORECASE).strip()
        words = [w for w in re.split(r"[^\w]+", clean_drug_name) if len(w) >= 3]
        search_term = f"%{words[0]}%" if words else f"%{raw_name}%"

        catalog_rows = []
        if db_conn is not None and words:
            query = (
                "SELECT ean_barcode, name, concentration, active_principle "
                "FROM anvisa_medication_catalog "
                "WHERE name ILIKE $1 OR active_principle ILIKE $1 LIMIT 5"
            )
            try:
                res = db_conn.fetch(query, search_term)
                if inspect.isawaitable(res):
                    catalog_rows = await res
                else:
                    catalog_rows = res
            except Exception as exc:
                logger.warning("[prescription_analyzer] Erro na consulta ANVISA: %s", exc)
                catalog_rows = []

        if catalog_rows:
            # Medicamento existe na base ANVISA: validar compatibilidade de dose
            compatible_row = None
            for row in catalog_rows:
                r_conc = row.get("concentration") or ""
                r_mg = parse_dosage_mg(r_conc)
                if dosage_mg is not None and r_mg is not None:
                    if math.isclose(dosage_mg, r_mg, abs_tol=0.01):
                        compatible_row = row
                        break
                else:
                    norm_c = normalize_text(r_conc).replace(" ", "")
                    norm_d = normalize_text(raw_dosage).replace(" ", "")
                    if norm_c and (norm_c in norm_d or norm_d in norm_c):
                        compatible_row = row
                        break

            if compatible_row:
                ean_barcode = compatible_row.get("ean_barcode")
                active_principle = compatible_row.get("active_principle")
            else:
                # Medicamento existe na ANVISA mas a concentração extraída é inexistente no mercado
                dosage_divergent = True
                reasons.append(
                    f"Concentração '{raw_dosage}' não existe no catálogo ANVISA para {words[0] if words else raw_name}."
                )
        else:
            # Medicamento não consta no catálogo referencial
            requires_human_fill = True
            reasons.append("Medicamento não localizado no catálogo ANVISA de referência.")

        # -------------------------------------------------------------
        # REGRA DE OURO: Tolerância Zero com Divergências e Incertezas
        # Se qualquer campo divergir, vem OBRIGATORIAMENTE VAZIO ("")
        # -------------------------------------------------------------
        if dosage_divergent:
            final_dosage = ""
            dosage_mg = None
            requires_human_fill = True
        else:
            final_dosage = raw_dosage

        if name_divergent:
            final_name = ""
            requires_human_fill = True

        if frequency_divergent:
            final_freq = ""
            final_suggested_times = []
            requires_human_fill = True

        if name_divergent or dosage_divergent:
            confidence_score = 0.0
        elif frequency_divergent or not catalog_rows:
            confidence_score = 0.5
        else:
            confidence_score = 1.0
            requires_human_fill = False

        divergence_reason = "; ".join(reasons) if reasons else None

        results.append(
            AnalyzedMedicationItem(
                name=final_name,
                name_divergent=name_divergent,
                dosage=final_dosage,
                dosage_divergent=dosage_divergent,
                dosage_mg=dosage_mg,
                frequency=final_freq,
                frequency_divergent=frequency_divergent,
                suggested_times=final_suggested_times,
                ean_barcode=ean_barcode,
                active_principle=active_principle,
                confidence_score=confidence_score,
                requires_human_fill=requires_human_fill,
                divergence_reason=divergence_reason,
            )
        )

    return results
