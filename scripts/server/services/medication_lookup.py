"""Identificação de medicamento no assistente do aplicativo (PR3, passo 3.2).

A partir da fala (nome e/ou horário), consulta o cadastro e as programações
do paciente (medications.schedule_time / schedule_times) e devolve:

- success: um único candidato claro (dados suficientes);
- clarification: mais de um candidato empatado — pede a escolha, nunca
  auto-seleciona (o modelo não decide identidade);
- missing_data: nenhum candidato — informa e orienta o cadastro no app.

NUNCA cria medicamento, dose ou prescrição. "Remédio das oito" só vale com
dados suficientes: sem nome e com vários remédios no mesmo horário, é
esclarecimento.
"""
import re
import unicodedata
from typing import Any, List, Optional

from services.operations import OperationContext, OperationResult, validate_actor

_STOPWORDS = {
    "tomei", "tomar", "tome", "tomou", "remedio", "remedios", "medicamento",
    "medicamentos", "minha", "minhas", "meu", "meus", "hora", "horas", "das",
    "as", "aos", "agora", "hoje", "tarde", "noite", "manha", "que", "para",
    "com", "pelo", "pela", "deste", "desta", "ja", "ainda", "vou", "preciso",
}

# Horários por extenso mais comuns no português brasileiro.
_HOUR_BY_WORD = {
    "meio dia": 12, "meiodia": 12, "uma": 1, "duas": 2, "tres": 3,
    "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14,
    "catorze": 14, "quinze": 15, "dezesseis": 16, "dezessete": 17,
    "dezoito": 18, "dezenove": 19, "vinte": 20, "vinte e uma": 21,
    "vinte e duas": 22, "vinte e tres": 23, "vinte e quatro": 24,
}


def _normalize(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


def detect_hour(norm: str) -> Optional[str]:
    """Horário na fala, em 'HH:MM'. Detecta '20h', '20:00', 'das oito' e
    'às 8' / 'das 8' (número puro precedido de preposição de hora)."""
    def formatted(hour, minute=0):
        if re.search(r"\b(?:tarde|noite)\b", norm) and 1 <= hour <= 11:
            hour += 12
        if hour == 12 and re.search(r"\b(?:madrugada|meia noite)\b", norm):
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    m = re.search(r"\b(\d{1,2})h(\d{2})\b", norm)
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        return formatted(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,2})\s*(?:h|hs|horas?)\b", norm)
    if m:
        hora = int(m.group(1))
        if 0 <= hora <= 23:
            return formatted(hora)
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", norm)
    if m:
        hora, minuto = int(m.group(1)), int(m.group(2))
        if 0 <= hora <= 23 and 0 <= minuto <= 59:
            return formatted(hora, minuto)
    # "às 8" / "das 8" / "para as 8": número puro após preposição de horário.
    # Sem 'h' explícito, só essas preposições valem como hora — evita ler
    # "comprimido de 8" (dosagem) como oito horas.
    m = re.search(r"\b(?:as|das|para as|pelas?)\s+(\d{1,2})\b", norm)
    if m:
        hora = int(m.group(1))
        if 0 <= hora <= 23:
            return formatted(hora)
    for word, hora in sorted(_HOUR_BY_WORD.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b(?:as|das|pelas?|para as)\s+" + re.escape(word) + r"\b", norm):
            return formatted(hora) if hora <= 23 else None
    return None


def _medication_matches(norm: str, row: Any) -> int:
    """Score de aderência da fala ao medicamento. 0 = não casa."""
    nome = _normalize(row["name"] or "")
    if not nome:
        return 0
    score = 0
    if nome in norm:
        score += 3
    else:
        palavras_nome = {p for p in nome.split() if len(p) >= 4}
        palavras_fala = set(re.findall(r"\b\w{4,}\b", norm)) - _STOPWORDS
        if palavras_nome & palavras_fala:
            score += 2
    return score


async def identify_medication(conn: Any, context: OperationContext, text: str,
                              *, hour: Optional[str] = None) -> OperationResult:
    """
    Identifica o medicamento por nome e/ou horário na fala do paciente.

    hour: horário explícito na chamada; se None, tenta detectar da fala.
    """
    denied = await validate_actor(conn, context)
    if denied:
        return denied
    norm = _normalize(text)
    hora = hour or detect_hour(norm)
    try:
        rows: List[Any] = await conn.fetch(
            """SELECT id, name, dosage_mg, schedule_time, schedule_times,
                      current_units
               FROM medications WHERE user_id = $1
               ORDER BY name""",
            context.patient_id,
        )
    except Exception as err:
        print(f"[Med Lookup] Falha ao consultar medicamentos: {err}")
        return OperationResult.unavailable(
            "Não consegui acessar seus medicamentos agora. Tente novamente em instantes.")

    if not rows:
        return OperationResult.missing_data(
            "Você ainda não tem remédios cadastrados. Cadastre no aplicativo "
            "Braga Saúde para que eu possa registrar a tomada aqui.",
            data={"hour": hora, "candidates": []})

    candidatos = []
    for row in rows:
        score = _medication_matches(norm, row)
        if hora:
            horarios = {row["schedule_time"]}
            horarios.update(h.strip() for h in (row["schedule_times"] or "").split(",") if h.strip())
            if hora in horarios:
                score += 2
        if score > 0:
            candidatos.append((score, row))

    if not candidatos:
        pista = f" no horário {hora}" if hora else ""
        return OperationResult.missing_data(
            f"Não encontrei um remédio cadastrado{pista} que combine com o que "
            f"você disse. Você pode cadastrar no aplicativo Braga Saúde; depois "
            f"eu registro a tomada aqui.",
            data={"hour": hora, "candidates": []})

    candidatos.sort(key=lambda par: -par[0])
    topo = candidatos[0][0]
    empatados = [row for score, row in candidatos if score == topo]
    if len(empatados) > 1:
        lista = "; ".join(
            f"{row['name']} ({row['schedule_time']})" for row in empatados)
        return OperationResult.clarification(
            "Encontrei mais de um remédio que combina. Qual é: "
            + lista + "?",
            data={"hour": hora, "candidates": [
                {"id": str(row["id"]), "name": row["name"],
                 "schedule_time": row["schedule_time"]} for row in empatados]})

    row = empatados[0]
    return OperationResult.success(
        f"Encontrei {row['name']} (horário {row['schedule_time']}).",
        data={"medication_id": str(row["id"]), "name": row["name"],
              "schedule_time": row["schedule_time"], "hour": hora,
              "current_units": row.get("current_units")})
