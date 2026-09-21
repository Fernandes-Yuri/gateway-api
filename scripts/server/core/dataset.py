"""Curadoria do dataset de fine-tuning (futuro Qwen Braga).

Extraido do monolito server.py na Fase A passo 6
(doc 11_PLANO_MODULARIZACAO_E_TTS.md).

log_fine_tuning_sample grava o par de dialogo higienizado em formato
ChatML/ShareGPT no dataset_braga.jsonl. PII e removida antes da gravacao
(core.text.sanitize_pii) — o arquivo nunca contem nome, telefone, CPF ou
e-mail do paciente. A categoria clinica (detect_clinical_category) alimenta
o filtro de curadoria humana: amostras de emergencia sao priorizadas.
"""
import json
import uuid
from datetime import datetime
from typing import Optional

from core.text import sanitize_pii


def detect_clinical_category(text: str, action: Optional[str] = None) -> str:
    """Detecta a categoria clinica da interacao para curadoria do fine-tuning."""
    if action == "EMERGENCIA":
        return "emergency"
    if action in ("REGISTRAR_PRESSAO", "CONSULTAR_METRICAS"):
        return "vitals_bp"
    if action == "REGISTRAR_GLICEMIA":
        return "vitals_glucose"
    if action == "REGISTRAR_AGUA":
        return "hydration"
    if action == "REGISTRAR_BATIMENTOS":
        return "vitals_heartrate"
    if action == "REGISTRAR_OXIGENACAO":
        return "vitals_oxygen"
    if action in ("BUSCAR_ALIMENTO", "ABRIR_LISTA_COMPRAS"):
        return "nutrition"

    lower = text.lower()
    if any(w in lower for w in ["pressão", "sistólica", "diastólica", "12 por 8", "12/8", "pressao"]):
        return "vitals_bp"
    if any(w in lower for w in ["glicemia", "glicose", "diabetes", "insulina", "açúcar"]):
        return "vitals_glucose"
    if any(w in lower for w in ["água", "agua", "hidratar", "hidratação", "copo"]):
        return "hydration"
    if any(w in lower for w in ["batimentos", "frequência cardíaca", "pulso", "bpm"]):
        return "vitals_heartrate"
    if any(w in lower for w in ["spo2", "oxigenação", "saturação", "oxigênio", "oximetro"]):
        return "vitals_oxygen"
    if any(w in lower for w in ["dor no peito", "falta de ar", "desmaio", "samu", "192", "emergência"]):
        return "emergency"
    if any(w in lower for w in ["posto de saúde", "posto", "ubs", "sus", "médico", "consulta"]):
        return "universal_triage"
    return "general_care"


def log_fine_tuning_sample(
    user_prompt: str,
    assistant_reply: str,
    source: str = "app_chat",
    system_prompt: Optional[str] = None,
    user_name: Optional[str] = None,
    action: Optional[str] = None,
    action_params: Optional[dict] = None,
):
    """
    Grava o par de dialogo higienizado em formato ChatML/ShareGPT
    no arquivo dataset_braga.jsonl para o futuro fine-tuning.
    """
    try:
        sanitized_user = sanitize_pii(user_prompt, user_name)
        sanitized_assistant = sanitize_pii(assistant_reply, user_name)

        # Ignora se vazio ou trivial
        if len(sanitized_user.strip()) < 2 or len(sanitized_assistant.strip()) < 2:
            return

        category = detect_clinical_category(sanitized_user, action)

        entry = {
            "id": f"braga-ft-{uuid.uuid4().hex[:12]}",
            "timestamp": datetime.utcnow().isoformat(),
            "source": source,
            "category": category,
            "messages": [
                {"role": "system", "content": system_prompt or _state.system_prompt},
                {"role": "user", "content": sanitized_user},
                {"role": "assistant", "content": sanitized_assistant}
            ],
            "metadata": {
                "has_action": bool(action and action != "CONVERSA"),
                "action": action or "CONVERSA",
                "params": action_params or {},
                "curated": False
            }
        }

        with open(_state.dataset_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Dataset Logging Warning] Falha ao registrar amostra para fine-tuning: {e}")


class _AppState:
    """Porta das dependencias do monolito (SYSTEM_PROMPT e o caminho do
    dataset, que o server resolve a partir do env). Preenchida por server.py
    no startup."""

    system_prompt = ""
    dataset_path = "dataset_braga.jsonl"


_state = _AppState()


def configure_state(*, system_prompt="", dataset_path="dataset_braga.jsonl"):
    """Chamado por server.py ao subir a aplicacao."""
    _state.system_prompt = system_prompt
    _state.dataset_path = dataset_path
