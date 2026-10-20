"""Mente LLM do ORB em dois passos (Fase 3).

Passo 1 — classificador barato: 1 token, prompt curto, decide apenas
a intenção (nunca escreve a resposta, nunca inventa número).
Passo 2 — gerador de fala livre SOMENTE para CONVERSA; demais ações
viram template (métricas falam com dados reais do Postgres).

O envelope {fala, acao, parametros} que o app consome é montado aqui,
então o cliente Android não muda.
"""

import json
import re
from services.emergency_reply import emergency_reply
# AUD-G(B): PERSONA importado mas nunca referenciado — a persona real entra
# via build_conversation_system (conversation_policy).
from conversation_policy import APP_SYSTEM, build_conversation_system

LABELS = ("CONVERSA", "CONSULTAR_METRICAS", "LEMBRETES", "EXAMES", "EMERGENCIA")

# Prioridade quando o modelo cospe mais de um rótulo.
_LABEL_PRIORITY = ("EMERGENCIA", "CONSULTAR_METRICAS", "LEMBRETES", "EXAMES",
                   "ABRIR_LISTA_COMPRAS", "REGISTRAR_PRESSAO", "REGISTRAR_GLICEMIA",
                   "REGISTRAR_AGUA", "REGISTRAR_BATIMENTOS", "REGISTRAR_OXIGENACAO",
                   "CONVERSA")

# Rótulos fora do contrato do canal nunca saem do classificador (B1).

CLASSIFY_PROMPT = (
    "Você é um classificador. Dada a mensagem do usuário, responda com UMA palavra: "
    "CONVERSA, CONSULTAR_METRICAS, LEMBRETES, EXAMES ou EMERGENCIA. "
    "CONSULTAR_METRICAS só se pedir explicitamente para ver relatório ou histórico de medições. "
    "LEMBRETES só se pedir lembretes de remédio. EXAMES só se pedir exames. "
    "EMERGENCIA só em relato atual de sintomas graves ou pedido de socorro. Pergunta educativa, negação e exemplo hipotético são CONVERSA. "
    "Todo o resto, inclusive fofoca cotidiana e dúvidas gerais, é CONVERSA. "
    "Responda só a palavra, sem ponto final."
)

SPEAK_SYSTEM = APP_SYSTEM

FIXED_FALAS = {
    "LEMBRETES": "Toque em confirmar para abrir seus lembretes de remédios.",
    "EXAMES": "Toque em confirmar para abrir seus exames.",
    "EMERGENCIA": emergency_reply(),
}


def parse_label(text, channel: str = "chat") -> str:
    """Extrai o rótulo da resposta do classificador; padrão seguro: CONVERSA."""
    if not text or not isinstance(text, str):
        return "CONVERSA"
    try:
        from orb_actions import actions_for_channel
        allowed = set(actions_for_channel(channel)) | {"CONVERSA"}
    except Exception:
        allowed = set(_LABEL_PRIORITY)
    upper = text.upper()
    for label in _LABEL_PRIORITY:
        if label in allowed and re.search(r"\b%s\b" % label, upper):
            return label
    return "CONVERSA"


def build_classify_payload(model: str, user_text: str, channel: str = "chat") -> dict:
    try:
        from orb_actions import actions_for_channel
        labels = ", ".join(actions_for_channel(channel))
    except Exception:
        labels = "CONVERSA, CONSULTAR_METRICAS, LEMBRETES, EXAMES, EMERGENCIA"
    system = CLASSIFY_PROMPT + f" Ações com executor neste canal: {labels}."
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "max_tokens": 12,
    }


def build_speak_payload(model: str, history: list, temperature: float, max_tokens: int, preferences=None) -> dict:
    return {
        "model": model,
        "messages": [{"role": "system", "content": build_conversation_system('chat', preferences)}] + history,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }


def metricas_fala(row) -> str:
    """Fala de CONSULTAR_METRICAS a partir da linha real do Postgres (ou None)."""
    if not row:
        return "Você pode ver seu relatório de saúde na tela de relatório do aplicativo."
    parts = []
    try:
        sys_ = row.get("systolic_pressure") if isinstance(row, dict) else row["systolic_pressure"]
        dia_ = row.get("diastolic_pressure") if isinstance(row, dict) else row["diastolic_pressure"]
        glu_ = row.get("glucose_level") if isinstance(row, dict) else row["glucose_level"]
    except Exception:
        return "Você pode ver seu relatório de saúde na tela de relatório do aplicativo."
    if sys_ and dia_:
        parts.append(f"pressão {sys_} por {dia_}")
    if glu_:
        parts.append(f"glicemia {glu_}")
    if not parts:
        return "Você pode ver seu relatório de saúde na tela de relatório do aplicativo."
    return (
        f"Seu último registro: {', '.join(parts)}. "
        "Você pode ver o relatório completo na tela de relatório do aplicativo."
    )


def compose_envelope(fala: str, acao: str, parametros: dict) -> str:
    """Monta o JSON estrito que o app espera."""
    return json.dumps(
        {"fala": fala, "acao": acao, "parametros": parametros or {}},
        ensure_ascii=False,
    )


def assistant_fala(content: str) -> str:
    """Extrai só a fala de um envelope antigo do histórico.

    O modelo aprende o formato pelo histórico: se ele vir JSON, ele
    cospe JSON (regurgitação). Histórico limpo = fala em texto puro.
    """
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            fala = parsed.get("fala")
            if isinstance(fala, str) and fala.strip():
                return fala.strip()
    except Exception:
        pass
    return content
