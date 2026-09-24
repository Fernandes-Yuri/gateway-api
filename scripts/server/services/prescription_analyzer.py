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
from typing import NamedTuple, Optional

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


class ReconciliationResult(NamedTuple):
    medications: list[AnalyzedMedicationItem]
    total_pages_analyzed: int = 1
    duplicates_merged_count: int = 0



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


class PosologyResult(NamedTuple):
    interval_hours: Optional[int]
    daily_doses_count: int
    suggested_times: list[str]
    meal_context: Optional[str] = None


def infer_meal_context(text: str) -> Optional[str]:
    """Identifica o contexto de refeição a partir do texto."""
    if not text:
        return None
    f = normalize_text(text)
    if any(k in f for k in ["jejum", "antes do cafe", "antes de cafe", "antes cafe", "em jejum"]):
        return "JEJUM"
    if any(k in f for k in ["apos o cafe", "apos cafe", "depois do cafe", "depois cafe", "com o cafe", "com cafe"]):
        return "APOS_CAFE"
    if any(k in f for k in ["antes do almoco", "antes de almoco", "antes almoco"]):
        return "ANTES_ALMOCO"
    if any(k in f for k in ["apos o almoco", "apos almoco", "depois do almoco", "depois almoco", "com o almoco", "com almoco", "durante o almoco", "durante almoco"]):
        return "APOS_ALMOCO"
    if any(k in f for k in ["antes do jantar", "antes de jantar", "antes jantar"]):
        return "ANTES_JANTAR"
    if any(k in f for k in ["apos o jantar", "apos jantar", "depois do jantar", "depois jantar", "com o jantar", "com jantar", "durante o jantar", "durante jantar"]):
        return "APOS_JANTAR"
    if any(k in f for k in ["ao deitar", "a noite", "dormir", "noturno"]):
        return "AO_DEITAR"
    return None


def infer_posology(frequency: str) -> PosologyResult:
    """Infere (interval_hours, daily_doses_count, suggested_times, meal_context) baseados na frequência/posologia.

    Padrões estritos (SaMD RDC 657/2022):
    - 6/6h, 6 em 6 horas, a cada 6 horas, 4x ao dia -> interval=6, doses=4, times=["06:00", "12:00", "18:00", "00:00"]
    - 8/8h, 8 em 8 horas, a cada 8 horas, 3x ao dia -> interval=8, doses=3, times=["08:00", "16:00", "00:00"]
    - 12/12h, 12 em 12 horas, a cada 12 horas, 2x ao dia -> interval=12, doses=2, times=["08:00", "20:00"]
    - 24/24h, 1x ao dia, manhã -> interval=24, doses=1, times=["08:00"]
    - Contextos de refeição dedicados:
      * JEJUM / Antes do café -> ["07:00"]
      * APOS_CAFE -> ["08:00"]
      * ANTES_ALMOCO -> ["11:30"]
      * APOS_ALMOCO -> ["12:30"]
      * ANTES_JANTAR -> ["18:30"]
      * APOS_JANTAR -> ["19:30"]
      * AO_DEITAR / À noite -> ["22:00"]
    """
    f = normalize_text(frequency)
    if not f:
        return PosologyResult(None, 1, [], None)

    meal = infer_meal_context(f)

    # 1. Intervalos de alta dosagem / frequência horária
    if any(k in f for k in ["6 em 6", "6/6", "a cada 6", "4x", "4 vezes", "4 comprimidos"]):
        return PosologyResult(6, 4, ["06:00", "12:00", "18:00", "00:00"], meal)

    if any(k in f for k in ["8 em 8", "8/8", "a cada 8", "3x", "3 vezes", "3 comprimidos"]):
        return PosologyResult(8, 3, ["08:00", "16:00", "00:00"], meal)

    if any(k in f for k in ["12 em 12", "12/12", "a cada 12", "2x", "2 vezes", "2 comprimidos"]):
        return PosologyResult(12, 2, ["08:00", "20:00"], meal)

    # 2. Contexto de refeição com horários dedicados
    if meal == "JEJUM":
        return PosologyResult(24, 1, ["07:00"], meal)
    if meal == "APOS_CAFE":
        return PosologyResult(24, 1, ["08:00"], meal)
    if meal == "ANTES_ALMOCO":
        return PosologyResult(24, 1, ["11:30"], meal)
    if meal == "APOS_ALMOCO":
        return PosologyResult(24, 1, ["12:30"], meal)
    if meal == "ANTES_JANTAR":
        return PosologyResult(24, 1, ["18:30"], meal)
    if meal == "APOS_JANTAR":
        return PosologyResult(24, 1, ["19:30"], meal)
    if meal == "AO_DEITAR":
        return PosologyResult(24, 1, ["22:00"], meal)

    # 3. 24/24h / 1x ao dia / manhã
    if any(k in f for k in ["24 em 24", "24/24", "24h", "a cada 24", "1x", "1 vez", "dia", "manha", "manhã"]):
        return PosologyResult(24, 1, ["08:00"], meal)

    return PosologyResult(None, 1, ["08:00"], meal)


def infer_suggested_times(frequency: str) -> list[str]:
    """Infere horários sugeridos baseados na descrição de posologia/frequência."""
    return infer_posology(frequency).suggested_times


def infer_treatment_duration(text: str) -> Optional[int]:
    """Detecta duração do tratamento caso mencionada (ex: 'por 7 dias', 'durante 14 dias')."""
    if not text:
        return None
    norm = normalize_text(text)

    # Dias (ex: 'por 7 dias', 'durante 14 dias', 'uso por 10 dias')
    m_days = re.search(r"(?:por|durante|uso\s+por|tratamento\s+por)\s+(\d+)\s*(?:dias|dia\b)", norm)
    if m_days:
        return int(m_days.group(1))

    # Semanas (ex: 'por 1 semana', 'durante 2 semanas')
    m_weeks = re.search(r"(?:por|durante|uso\s+por|tratamento\s+por)\s+(\d+)\s*(?:semanas|semana\b)", norm)
    if m_weeks:
        return int(m_weeks.group(1)) * 7

    # Meses (ex: 'por 1 mes', 'durante 3 meses')
    m_months = re.search(r"(?:por|durante|uso\s+por|tratamento\s+por)\s+(\d+)\s*(?:meses|mes\b)", norm)
    if m_months:
        return int(m_months.group(1)) * 30

    return None


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
    valid_mimes = ["image/jpeg", "image/png", "image/webp", "application/pdf"]
    mime = content_type if content_type in valid_mimes else "image/jpeg"
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

    Se for PDF: extrai texto direto de todas as páginas via PyMuPDF (fitz).
    Se for PDF escaneado (sem camada textual): renderiza cada página (limitado a 10 páginas) como imagem e faz OCR.
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
            has_digital_text = any(bool(page.get_text().strip()) for page in doc)
            if has_digital_text:
                extracted = "\n".join(
                    f"--- PÁGINA {i+1} ---\n{page.get_text()}" for i, page in enumerate(doc)
                ).strip()
                if extracted:
                    return extracted
            # PDF escaneado (sem camada textual): renderiza cada página (limitado a 10 páginas)
            ocr_parts: list[str] = []
            for i, page in enumerate(doc[:10]):
                pix = page.get_pixmap()
                page_bytes = pix.tobytes("png")
                page_ocr = await _call_vision_ocr(page_bytes)
                if page_ocr:
                    ocr_parts.append(f"--- PÁGINA {i+1} ---\n{page_ocr}")
            if ocr_parts:
                return "\n".join(ocr_parts).strip()
        except Exception as exc:
            logger.warning("[extract_raw_ocr_text] Erro ao extrair texto de PDF via PyMuPDF: %s", exc)

    return await _call_vision_ocr(image_bytes)


async def _extract_single_image_structured(image_bytes: bytes, content_type: str = "image/jpeg") -> list[dict]:
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
        '      "frequency_interval_hours": 12,\n'
        '      "daily_doses_count": 2,\n'
        '      "treatment_duration_days": 7,\n'
        '      "instructions": "Instruções adicionais se houver",\n'
        '      "suggested_times": ["08:00", "20:00"],\n'
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


async def extract_structured_prescription(image_bytes: bytes, content_type: str = "image/jpeg") -> list[dict]:
    """Interpreta a imagem ou páginas de PDF e extrai estruturadamente medicamentos, doses e horários (Motor 2)."""
    is_pdf = content_type == "application/pdf" or image_bytes.startswith(b"%PDF")
    if is_pdf:
        all_medications: list[dict] = []
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
            doc = fitz.open(stream=image_bytes, filetype="pdf")
            for page_idx, page in enumerate(doc[:10]):
                pix = page.get_pixmap()
                page_png = pix.tobytes("png")
                page_meds = await _extract_single_image_structured(page_png, "image/png")
                for med in page_meds:
                    med["source_page"] = page_idx + 1
                    all_medications.append(med)
            return all_medications
        except Exception as exc:
            logger.warning("[extract_structured_prescription] Falha ao renderizar PDF para visão: %s", exc)

    meds = await _extract_single_image_structured(image_bytes, content_type)
    for med in meds:
        if "source_page" not in med:
            med["source_page"] = 1
    return meds


async def reconcile_and_verify(
    structured_items: list[dict],
    raw_text: str,
    db_conn,
    total_pages: Optional[int] = None,
) -> ReconciliationResult:
    """Árbitro e Dupla Checagem contra Texto Bruto e Catálogo ANVISA com Deduplicação Anti-Cópias (Motor 3).

    Garante o princípio clínico de tolerância zero com ambiguidades e funde 1ª e 2ª vias idênticas.
    """
    merged_items: list[dict] = []
    seen_keys: dict[tuple[str, float], dict] = {}
    duplicates_merged_count = 0
    pages_seen: set[int] = set()

    for item in structured_items:
        current_page = item.get("source_page") or (item.get("source_pages") and item["source_pages"][0]) or 1
        pages_seen.add(current_page)

        raw_name = (item.get("name") or "").strip()
        raw_dosage = (item.get("dosage") or "").strip()

        # Chave canônica de unicidade
        canonical_name = re.sub(
            r"(potassica|potássica|cloridrato|maleato|sodica|sódica|comprimido|comprimidos|capsula|capsulas|gotas)",
            "",
            normalize_text(raw_name),
        ).strip()
        canonical_name = " ".join(canonical_name.split())
        canonical_dose = parse_dosage_mg(raw_dosage) or 0.0
        key = (canonical_name, canonical_dose)

        if canonical_name and key in seen_keys:
            existing = seen_keys[key]
            if current_page not in existing["source_pages"]:
                existing["source_pages"].append(current_page)
            duplicates_merged_count += 1
        else:
            item_copy = dict(item)
            item_copy["source_pages"] = [current_page]
            if canonical_name:
                seen_keys[key] = item_copy
            merged_items.append(item_copy)

    total_pages_analyzed = total_pages if total_pages is not None else (max(pages_seen) if pages_seen else 1)
    results: list[AnalyzedMedicationItem] = []

    for item in merged_items:
        raw_name = (item.get("name") or "").strip()
        raw_dosage = (item.get("dosage") or "").strip()
        raw_freq = (item.get("frequency") or "").strip()
        raw_suggested = item.get("suggested_times") or []
        caligrafia_duvidosa = bool(item.get("caligrafia_duvidosa"))
        source_pages = item.get("source_pages", [1])

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
        # 3. Verificação da Frequência / Posologia e Duração
        # -------------------------------------------------------------
        if not raw_freq or caligrafia_duvidosa or any(w in normalize_text(raw_freq) for w in ["incert", "duvid", "ambig"]):
            frequency_divergent = True
            final_freq = ""
            final_suggested_times = []
            final_interval_hours = None
            final_daily_doses = 1
            final_duration_days = None
            requires_human_fill = True
            reasons.append("Frequência/posologia ausente ou duvidosa.")
        else:
            frequency_divergent = False
            final_freq = raw_freq
            search_freq = f"{raw_freq} {item.get('instructions', '')}".strip()
            posology = infer_posology(search_freq)
            inferred_interval = posology.interval_hours
            inferred_doses = posology.daily_doses_count
            inferred_times = posology.suggested_times
            inferred_meal = posology.meal_context

            # Intervalo em horas
            raw_interval = item.get("frequency_interval_hours")
            if raw_interval is not None:
                try:
                    final_interval_hours = int(raw_interval)
                except (ValueError, TypeError):
                    final_interval_hours = inferred_interval
            else:
                final_interval_hours = inferred_interval

            # Doses diárias
            raw_doses = item.get("daily_doses_count")
            if raw_doses is not None:
                try:
                    final_daily_doses = int(raw_doses)
                except (ValueError, TypeError):
                    final_daily_doses = inferred_doses
            else:
                final_daily_doses = inferred_doses

            # Contexto de refeição
            final_meal_context = item.get("meal_context") or inferred_meal or infer_meal_context(item.get("instructions", ""))

            # Horários sugeridos
            final_suggested_times = raw_suggested if raw_suggested else inferred_times

            # Duração do tratamento (ex: por 7 dias, durante 14 dias)
            raw_duration = item.get("treatment_duration_days")
            if raw_duration is not None:
                try:
                    final_duration_days = int(raw_duration)
                except (ValueError, TypeError):
                    final_duration_days = None
            else:
                search_ctx = f"{raw_freq} {item.get('instructions', '')} {item.get('duration', '')}"
                final_duration_days = infer_treatment_duration(search_ctx)
                if not final_duration_days and raw_text and raw_name:
                    for line in raw_text.splitlines():
                        if is_name_in_raw_text(raw_name, line):
                            dur = infer_treatment_duration(line)
                            if dur:
                                final_duration_days = dur
                                break

        # -------------------------------------------------------------
        # 4. Verificação 2: Catálogo ANVISA
        # -------------------------------------------------------------
        ean_barcode: Optional[str] = None
        active_principle: Optional[str] = None
        anvisa_registration_number: Optional[str] = None
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
                anvisa_registration_number = (
                    compatible_row.get("anvisa_registration_number")
                    or item.get("anvisa_registration_number")
                )
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

        if not anvisa_registration_number and not name_divergent:
            anvisa_registration_number = item.get("anvisa_registration_number")

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
            anvisa_registration_number = None
            requires_human_fill = True

        if frequency_divergent:
            final_freq = ""
            final_suggested_times = []
            final_interval_hours = None
            final_daily_doses = 1
            final_duration_days = None
            final_meal_context = None
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
                frequency_interval_hours=final_interval_hours,
                daily_doses_count=final_daily_doses,
                treatment_duration_days=final_duration_days,
                meal_context=final_meal_context,
                suggested_times=final_suggested_times,
                ean_barcode=ean_barcode,
                anvisa_registration_number=anvisa_registration_number,
                active_principle=active_principle,
                confidence_score=confidence_score,
                requires_human_fill=requires_human_fill,
                divergence_reason=divergence_reason,
                source_pages=source_pages,
            )
        )

    return ReconciliationResult(
        medications=results,
        total_pages_analyzed=total_pages_analyzed,
        duplicates_merged_count=duplicates_merged_count,
    )


async def analyze_pdf_prescription(pdf_bytes: bytes, db_conn) -> ReconciliationResult:
    """Pipeline especializado para receitas médicas digitais em PDF (Single-call Gemini + Enriquecimento ANVISA).

    Características clínicas e de arquitetura:
    - Tipografia digital computacional (sem caligrafia manual).
    - Envio do PDF nativo em uma única chamada (1 cota RPM).
    - Extração de múltiplos medicamentos estruturados simultâneos.
    - Deduplicação de cópias/vias (1ª via paciente e 2ª via farmácia).
    - Enriquecimento não-destrutivo com ANVISA (preserva nome/dose mesmo sem catálogo).
    """
    total_pages = 1
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)
    except Exception as exc:
        logger.warning("[analyze_pdf_prescription] Erro ao contar páginas do PDF: %s", exc)
        total_pages = 1

    prompt = (
        "Você é um assistente clínico especializado em extrair receitas médicas digitais.\n"
        "Analise o PDF e extraia TODOS os medicamentos prescritos.\n\n"
        "REGRAS OBRIGATÓRIAS:\n"
        "1. MÚLTIPLOS MEDICAMENTOS: Uma receita contém frequentemente múltiplos medicamentos (Item 1, 2, 3...). "
        "Extraia TODOS os medicamentos no array 'medications'. Nunca pare no primeiro!\n"
        "2. DEDUPLICAÇÃO DE VIAS: Se o PDF contiver cópias (ex: 1ª via paciente e 2ª via farmácia), "
        "identifique os remédios idênticos e liste as páginas onde aparecem (ex: source_pages: [1, 2]).\n"
        "3. CONTEXTO DE REFEIÇÃO: Identifique se o remédio deve ser tomado:\n"
        "   - Em jejum / Antes do café -> meal_context: \"JEJUM\", horário sugerido: \"07:00\"\n"
        "   - Após o café -> meal_context: \"APOS_CAFE\", horário sugerido: \"08:00\"\n"
        "   - Antes do almoço -> meal_context: \"ANTES_ALMOCO\", horário sugerido: \"11:30\"\n"
        "   - Com o almoço / Após o almoço -> meal_context: \"APOS_ALMOCO\", horário sugerido: \"12:30\"\n"
        "   - Antes do jantar -> meal_context: \"ANTES_JANTAR\", horário sugerido: \"18:30\"\n"
        "   - Após o jantar -> meal_context: \"APOS_JANTAR\", horário sugerido: \"19:30\"\n"
        "   - Ao deitar / À noite -> meal_context: \"AO_DEITAR\", horário sugerido: \"22:00\"\n\n"
        "Retorne EXCLUSIVAMENTE um objeto JSON no formato:\n"
        "{\n"
        '  "medications": [\n'
        "    {\n"
        '      "name": "Nome do medicamento",\n'
        '      "dosage": "Dose (ex: 50mg, 10mg)",\n'
        '      "frequency": "Posologia/frequência (ex: 1x ao dia, de 12 em 12 horas)",\n'
        '      "frequency_interval_hours": 12,\n'
        '      "daily_doses_count": 2,\n'
        '      "treatment_duration_days": 7,\n'
        '      "meal_context": "APOS_CAFE",\n'
        '      "instructions": "Instruções adicionais se houver",\n'
        '      "suggested_times": ["08:00", "20:00"],\n'
        '      "source_pages": [1]\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    extracted_items = []
    if GEMINI_API_KEY:
        gemini_res = await _call_gemini_vision(pdf_bytes, prompt, content_type="application/pdf")
        if gemini_res:
            extracted_items = _parse_json_medications(gemini_res)

    if not extracted_items:
        # Fallback resiliente: extrai texto direto com PyMuPDF e consulta modelo de texto
        raw_text = await extract_raw_ocr_text(pdf_bytes, "receita.pdf")
        if GROQ_API_KEY and raw_text:
            try:
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                payload = {
                    "model": GROQ_MODEL_VISION,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Texto da receita médica:\n{raw_text}"},
                    ],
                    "temperature": 0.0,
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(GROQ_API_URL, json=payload, headers=headers)
                    if resp.status_code == 200:
                        content = resp.json()["choices"][0]["message"]["content"]
                        extracted_items = _parse_json_medications(content)
            except Exception as exc:
                logger.warning("[analyze_pdf_prescription] Falha no fallback Groq: %s", exc)

    # Deduplicação de vias / cópias
    merged_items: list[dict] = []
    seen_keys: dict[tuple[str, float], dict] = {}
    duplicates_merged_count = 0

    for item in extracted_items:
        current_pages = item.get("source_pages") or ([item.get("source_page")] if item.get("source_page") else [1])
        raw_name = (item.get("name") or "").strip()
        raw_dosage = (item.get("dosage") or "").strip()

        canonical_name = re.sub(
            r"(potassica|potássica|cloridrato|maleato|sodica|sódica|comprimido|comprimidos|capsula|capsulas|gotas)",
            "",
            normalize_text(raw_name),
        ).strip()
        canonical_name = " ".join(canonical_name.split())
        canonical_dose = parse_dosage_mg(raw_dosage) or 0.0
        key = (canonical_name, canonical_dose)

        if canonical_name and key in seen_keys:
            existing = seen_keys[key]
            for p in current_pages:
                if p not in existing["source_pages"]:
                    existing["source_pages"].append(p)
            duplicates_merged_count += 1
        else:
            item_copy = dict(item)
            item_copy["source_pages"] = list(current_pages)
            if canonical_name:
                seen_keys[key] = item_copy
            merged_items.append(item_copy)

    results: list[AnalyzedMedicationItem] = []
    for item in merged_items:
        raw_name = (item.get("name") or "").strip()
        raw_dosage = (item.get("dosage") or "").strip()
        raw_freq = (item.get("frequency") or "").strip()
        source_pages = item.get("source_pages") or [1]
        dosage_mg = parse_dosage_mg(raw_dosage)

        # Enriquecimento com ANVISA (opcional e não destrutivo: mantém nome e dosagem)
        ean_barcode = None
        active_principle = None
        anvisa_registration_number = item.get("anvisa_registration_number")

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
                logger.warning("[analyze_pdf_prescription] Erro na consulta ANVISA: %s", exc)
                catalog_rows = []

        if catalog_rows:
            for row in catalog_rows:
                r_conc = row.get("concentration") or ""
                r_mg = parse_dosage_mg(r_conc)
                if dosage_mg is not None and r_mg is not None:
                    if math.isclose(dosage_mg, r_mg, abs_tol=0.01):
                        ean_barcode = row.get("ean_barcode")
                        active_principle = row.get("active_principle")
                        anvisa_registration_number = row.get("anvisa_registration_number") or anvisa_registration_number
                        break
                else:
                    norm_c = normalize_text(r_conc).replace(" ", "")
                    norm_d = normalize_text(raw_dosage).replace(" ", "")
                    if norm_c and (norm_c in norm_d or norm_d in norm_c):
                        ean_barcode = row.get("ean_barcode")
                        active_principle = row.get("active_principle")
                        anvisa_registration_number = row.get("anvisa_registration_number") or anvisa_registration_number
                        break

        # Posologia, Refeição e Duração
        full_posology_text = f"{raw_freq} {item.get('instructions', '')}".strip()
        posology = infer_posology(full_posology_text)
        raw_interval = item.get("frequency_interval_hours")
        final_interval = int(raw_interval) if raw_interval is not None else posology.interval_hours
        raw_doses = item.get("daily_doses_count")
        final_doses = int(raw_doses) if raw_doses is not None else posology.daily_doses_count
        final_suggested_times = item.get("suggested_times") or posology.suggested_times
        final_meal_context = item.get("meal_context") or posology.meal_context or infer_meal_context(item.get("instructions", ""))
        raw_duration = item.get("treatment_duration_days")
        final_duration = int(raw_duration) if raw_duration is not None else infer_treatment_duration(
            full_posology_text
        )

        results.append(
            AnalyzedMedicationItem(
                name=raw_name,
                name_divergent=False,
                dosage=raw_dosage,
                dosage_divergent=False,
                dosage_mg=dosage_mg,
                frequency=raw_freq,
                frequency_divergent=False,
                frequency_interval_hours=final_interval,
                daily_doses_count=final_doses,
                treatment_duration_days=final_duration,
                meal_context=final_meal_context,
                suggested_times=final_suggested_times,
                ean_barcode=ean_barcode,
                anvisa_registration_number=anvisa_registration_number,
                active_principle=active_principle,
                confidence_score=1.0 if ean_barcode else 0.95,
                requires_human_fill=not bool(raw_name),
                divergence_reason=None,
                source_pages=source_pages,
            )
        )

    return ReconciliationResult(
        medications=results,
        total_pages_analyzed=total_pages,
        duplicates_merged_count=duplicates_merged_count,
    )

