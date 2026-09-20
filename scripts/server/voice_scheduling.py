"""Agendamento de consulta por voz no MODO CUIDADOR (D50/D51).

O cuidador fala com o assistente: "Agenda uma consulta com o cardiologista
quinta-feira às 14h". O fluxo e RASCUNHO + CONFIRMACAO (D51):

  1. deteta a intencao de agendar e extrai tipo/data/hora (regex, sem LLM);
  2. guarda um RASCUNHO em memoria (nao grava na agenda ainda);
  3. pergunta "Confirma?"; o usuario confirma por VOZ ("eu confirmo") ou
     tocando no botao Confirmar na tela;
  4. aceita correcao por voz ("na verdade e sexta as 15h");
  5. so na confirmacao grava na tabela consultations.

Rascunhos expiram em 5 minutos (DRAFT_TTL_S). Nada e gravado antes da
confirmacao — um erro de reconhecimento de voz nunca vira consulta marcada.
"""
import re
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
try:
    from zoneinfo import ZoneInfo
    _TZ_BRASIL = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - fallback sem zoneinfo (Windows antigo)
    _TZ_BRASIL = timezone(timedelta(hours=-3))  # BRT fixo

def agora_brasil() -> datetime:
    """Hora atual no fuso brasileiro (BRT, America/Sao_Paulo).

    AUD-G03: o público é 100% Brasil; o diálogo de voz ("quinta às 14h", "hoje")
    precisa ser resolvido e gravado em hora local, não em UTC — senão toda
    consulta fica 3h deslocada e "hoje"/"quinta" trocam de dia perto da meia-noite.
    """
    return datetime.now(_TZ_BRASIL)

# Estado de rascunho: scope_key -> (draft, expiry_ts)
_PENDING_SCHEDULES: Dict[str, Tuple[dict, float]] = {}
_DRAFT_TTL_S = 300  # 5 minutos — dialogo de voz e curto

# Dias da semana -> weekday() do Python (segunda=0 ... sexta=4, sabado=5,
# domingo=6).
_DIAS_SEMANA = {
    "domingo": 6, "dom": 6,
    "segunda-feira": 0, "segunda": 0, "seg": 0,
    "terca-feira": 1, "terça-feira": 1, "terca": 1, "terça": 1, "ter": 1,
    "quarta-feira": 2, "quarta": 2, "qua": 2,
    "quinta-feira": 3, "quinta": 3, "qui": 3,
    "sexta-feira": 4, "sexta": 4, "sex": 4,
    "sabado": 5, "sábado": 5, "sab": 5,
}

# Intencao de agendar — aceita "marcar no medico", "agendar consulta", etc.
_PADRAO_AGENDAR = re.compile(
    r"(agend\w*|marc\w*|coloque\w*|inscreve\w*)"
    r".{0,40}"
    r"(consult\w*|medic\w*|medico|especialist\w*|cardiolog\w*|ortoped\w*|"
    r"oftalmolog\w*|dermatolog\w*|neurolog\w*|psicolog\w*|nutricionist\w*|fisioterape\w*|"
    r"clinico geral|clinica|hospital|post\w*|ubs)",
    re.IGNORECASE | re.DOTALL,
)

# Tipo de consulta pela especialidade mencionada
_ESPECIALIDADES = [
    ("cardiolog", "Cardiologista"),
    ("ortoped", "Ortopedista"),
    ("oftalmolog", "Oftalmologista"),
    ("dermatolog", "Dermatologista"),
    ("neurolog", "Neurologista"),
    ("psicolog", "Psicólogo"),
    ("nutricionist", "Nutricionista"),
    ("fisioterape", "Fisioterapeuta"),
    ("clinico geral", "Clínico geral"),
    ("geriatra", "Geriatra"),
    ("endocrinolog", "Endocrinologista"),
    ("ginecolog", "Ginecologista"),
    ("urolog", "Urologista"),
    ("pediatra", "Pediatra"),
    ("post", "Posto de saúde"),
    ("ubs", "Posto de saúde (UBS)"),
]

_PADRAO_HORA = re.compile(r"(\d{1,2})\s*(?:h|hs|horas|:|hr)\s*(\d{0,2})?", re.IGNORECASE)
_PADRAO_DIA_NUMERO = re.compile(r"(?:dia\s*)?(\d{1,2})(?:\s*(?:de\s*)?([a-zç]+))?", re.IGNORECASE)
_PADRAO_DE_PRAZO = re.compile(r"(hoje|amanha|depois de amanha|proxima semana|semana que vem|"
                              r"proxim\w+\s*(?:segunda|terca|quarta|quinta|sexta|sabado|domingo))",
                              re.IGNORECASE)

_MESES = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "marco": 3, "mar": 3,
    "abril": 4, "abr": 4, "maio": 5, "junho": 6, "jun": 6, "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8, "setembro": 9, "set": 9, "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11, "dezembro": 12, "dez": 12,
}


def _sem_acento(s: str) -> str:
    """Normaliza acentos para os regexes nao dependerem de collation."""
    try:
        s = s.casefold()
    except Exception:
        return s
    return (s.replace("á", "a").replace("à", "a").replace("â", "a")
             .replace("ã", "a").replace("é", "e").replace("ê", "e")
             .replace("í", "i").replace("ó", "o").replace("ô", "o")
             .replace("õ", "o").replace("ú", "u").replace("ç", "c"))


def quer_agendar(texto: str) -> bool:
    """True se a fala do cuidador e uma intencao de agendar consulta."""
    if not texto:
        return False
    return bool(_PADRAO_AGENDAR.search(_sem_acento(texto)))


# --- D51: intents de confirmacao / correcao / cancelamento por voz ---

_PADRAO_CONFIRMAR = re.compile(
    r"\b(confirmo|confirmar|pode confirmar|esta confirmado|pode agendar|"
    r"agendar|pode marcar|eu confirmo|sim confirmo|confirmado)\b",
    re.IGNORECASE,
)

_PADRAO_CORRIGIR = re.compile(
    r"\b(na verdade|errado|errada|nao e|incorreto|muda|mudar|trocar|"
    r"outro dia|outra hora|refazer|ajustar)\b",
    re.IGNORECASE,
)

_PADRAO_CANCELAR = re.compile(
    r"\b(cancela|cancelar|esquece|deixa|nao quero|abortar|desistir)\b",
    re.IGNORECASE,
)


def quer_confirmar(texto: str) -> bool:
    """True se o usuario esta confirmando o rascunho por voz."""
    if not texto:
        return False
    baixa = _sem_acento(texto)
    # "nao confirmo" e correcao/cancelamento, nao confirmacao
    if re.search(r"\bnao\s+(confirmo|quero confirmar)\b", baixa):
        return False
    if _PADRAO_CORRIGIR.search(baixa) or _PADRAO_CANCELAR.search(baixa):
        return False
    return bool(_PADRAO_CONFIRMAR.search(baixa))


def quer_corrigir(texto: str) -> bool:
    """True se o usuario quer corrigir o rascunho."""
    if not texto:
        return False
    return bool(_PADRAO_CORRIGIR.search(_sem_acento(texto)))


def quer_cancelar(texto: str) -> bool:
    """True se o usuario quer abandonar o rascunho."""
    if not texto:
        return False
    return bool(_PADRAO_CANCELAR.search(_sem_acento(texto)))


def _chave_rascunho(uid_cuidador: str, patient_id: str) -> str:
    return f"{uid_cuidador}:{patient_id}"


def obter_rascunho(uid_cuidador: str, patient_id: str) -> Optional[dict]:
    """Retorna o rascunho ativo (nao expirado) ou None."""
    chave = _chave_rascunho(uid_cuidador, patient_id)
    alvo = _PENDING_SCHEDULES.get(chave)
    if not alvo:
        return None
    draft, expira = alvo
    if time.time() > expira:
        _PENDING_SCHEDULES.pop(chave, None)
        return None
    return draft


def descartar_rascunho(uid_cuidador: str, patient_id: str) -> None:
    _PENDING_SCHEDULES.pop(_chave_rascunho(uid_cuidador, patient_id), None)


def _guardar_rascunho(uid_cuidador: str, patient_id: str, draft: dict) -> None:
    _PENDING_SCHEDULES[_chave_rascunho(uid_cuidador, patient_id)] = (
        dict(draft), time.time() + _DRAFT_TTL_S,
    )


def extrair_especialidade(texto: str) -> str:
    baixa = _sem_acento(texto)
    for chave, nome in _ESPECIALIDADES:
        if chave in baixa:
            return nome
    return "Consulta médica"


def _normalizar_hora(hh: int, mm: int) -> tuple:
    """Hora 24h valida."""
    if hh > 23:
        # "14h" -> 14; mas "1400" mal-formado nao deve virar 14:00 cegamente
        if hh >= 100:
            hh2 = hh // 100
            mm2 = hh % 100
            if hh2 <= 23 and mm2 <= 59:
                return hh2, mm2
        return -1, -1
    if mm > 59:
        return -1, -1
    return hh, mm


def extrair_data_hora(texto: str, agora: datetime | None = None) -> tuple:
    """Extrai (datetime, string_amigavel) ou (None, None) se nao conseguir.

    Ordem de tentativa: dia da semana > prazo relativo > dia do mes > hora so.
    """
    if agora is None:
        agora = agora_brasil()

    baixa = _sem_acento(texto)

    # 1. Hora — usada por todos os caminhos
    hh, mm = -1, -1
    m = _PADRAO_HORA.search(baixa)
    if m:
        try:
            hh = int(m.group(1))
            mm = int(m.group(2)) if m.group(2) else 0
        except ValueError:
            hh, mm = -1, -1
        hh, mm = _normalizar_hora(hh, mm)

    # 2. Dia da semana (ex.: "quinta-feira")
    for nome, alvo_wd in _DIAS_SEMANA.items():
        if re.search(r"\b" + re.escape(nome) + r"\b", baixa):
            dias_ate = (alvo_wd - agora.weekday()) % 7
            if dias_ate == 0:
                dias_ate = 7  # "quinta" hoje = proxima quinta
            alvo = agora + timedelta(days=dias_ate)
            if hh >= 0:
                alvo = alvo.replace(hour=hh, minute=mm, second=0, microsecond=0)
                return alvo, alvo.strftime("%d/%m %Hh%M")
            return None, None  # dia sem hora -> nao agenda cego

    # 3. Prazo relativo
    m = _PADRAO_DE_PRAZO.search(baixa)
    if m:
        prazo = m.group(1).lower()
        dias = {"hoje": 0, "amanha": 1, "depois de amanha": 2,
                "proxima semana": 7, "semana que vem": 7}.get(prazo)
        if dias is None and prazo.startswith("proxim"):
            for nome, alvo_wd in _DIAS_SEMANA.items():
                if nome in prazo:
                    dias = (alvo_wd - agora.weekday()) % 7 or 7
                    break
        if dias is not None:
            alvo = agora + timedelta(days=dias)
            if hh >= 0:
                alvo = alvo.replace(hour=hh, minute=mm, second=0, microsecond=0)
                return alvo, alvo.strftime("%d/%m %Hh%M")
            return None, None
    # 4. Dia do mes (ex.: "dia 20", "20 de outubro")
    m = _PADRAO_DIA_NUMERO.search(baixa)
    if m:
        try:
            dia = int(m.group(1))
        except ValueError:
            dia = -1
        mes = None
        if m.group(2):
            mes = _MESES.get(m.group(2).lower())
        if 1 <= dia <= 31:
            ano = agora.year
            mes_alvo = mes if mes else agora.month
            # se o mes informado ja passou neste ano, vai para o ano que vem
            if mes and mes_alvo < agora.month:
                ano += 1
            if not mes and dia < agora.day:
                mes_alvo = agora.month + 1
                if mes_alvo > 12:
                    mes_alvo = 1
                    ano += 1
            try:
                alvo = datetime(ano, mes_alvo, dia, hh if hh >= 0 else 9,
                                mm if mm >= 0 else 0, tzinfo=_TZ_BRASIL)
                if hh >= 0:
                    return alvo, alvo.strftime("%d/%m %Hh%M")
                return None, None
            except ValueError:
                pass

    # 5. So hora (hoje)
    if hh >= 0:
        alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if alvo <= agora:
            alvo += timedelta(days=1)  # hora ja passou -> amanha
        return alvo, alvo.strftime("%d/%m %Hh%M")

    return None, None


async def agendar_por_voz(conn, uid_cuidador: str, patient_id: str,
                          texto_falado: str, caregiver_name: str = "") -> dict:
    """Ponto de entrada do dialogo de agendamento (D51: rascunho + confirmacao).

    Nunca grava direto: cria um rascunho e pede confirmacao. O usuario
    confirma por voz ("eu confirmo") ou pelo botao na tela
    (confirmar_rascunho_por_id). Aceita correcao por voz.
    """
    from api_identity import require_family_access
    await require_family_access(conn, uid_cuidador, patient_id)

    baixa = _sem_acento(texto_falado)

    # 1. Cancelamento explicito — descarta qualquer rascunho
    if quer_cancelar(texto_falado):
        descartar_rascunho(uid_cuidador, patient_id)
        return {"fala": "Agendamento cancelado. Nada foi marcado.",
                "acao": "AGENDAR_CANCELADO", "parametros": {}}

    # 2. Confirmacao por voz — grava o rascunho existente
    if quer_confirmar(texto_falado):
        return await _confirmar_rascunho(conn, uid_cuidador, patient_id,
                                         caregiver_name)

    # 3. Correcao do rascunho — refaz com os dados novos
    rascunho = obter_rascunho(uid_cuidador, patient_id)
    if rascunho and quer_corrigir(texto_falado):
        return await _montar_rascunho(conn, uid_cuidador, patient_id,
                                      texto_falado, caregiver_name,
                                      base=rascunho)

    # 4. Nova intencao de agendar
    if not quer_agendar(texto_falado):
        descartar_rascunho(uid_cuidador, patient_id)
        return {"fala": "Não entendi um agendamento. Pode repetir, por exemplo: "
                        "“Agenda uma consulta com o cardiologista quinta às 14h”?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    return await _montar_rascunho(conn, uid_cuidador, patient_id,
                                  texto_falado, caregiver_name)


async def _montar_rascunho(conn, uid_cuidador, patient_id, texto_falado,
                           caregiver_name, base=None):
    """Extrai dados, guarda o rascunho (sem gravar) e pede confirmacao."""
    titulo = extrair_especialidade(texto_falado)
    quando, _ = extrair_data_hora(texto_falado)

    # Correcao: dados ausidos na nova fala vem do rascunho anterior
    if base:
        if titulo == "Consulta médica":
            titulo = base.get("title", titulo)
        if quando is None:
            try:
                quando = datetime.fromisoformat(base["scheduledDate"])
            except Exception:
                quando = None

    if quando is None:
        return {"fala": f"Para agendar a consulta com {titulo.lower()}, preciso da data e "
                        "da hora. Pode dizer, por exemplo: “quinta-feira às 14h”?",
                "acao": "AGENDAR_FALHA", "parametros": {"titulo": titulo}}

    if quando < agora_brasil():
        return {"fala": "Essa data já passou. Quer agendar para outra data?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    draft = {
        "id": str(uuid.uuid4()),
        "title": titulo,
        "scheduledDate": quando.isoformat(),
        "caregiver_name": caregiver_name or "Cuidador",
    }
    _guardar_rascunho(uid_cuidador, patient_id, draft)

    data_pt = quando.strftime("%d/%m/%Y às %Hh%M")
    return {
        "fala": f"Preenchi aqui para você: {titulo}, no dia {data_pt}. "
                "Deseja confirmar? Basta tocar em Confirmar na tela ou me dizer "
                "“eu confirmo”. Se algo estiver errado, pode me corrigir.",
        "acao": "AGENDAR_RASCUNHO",
        "parametros": dict(draft),
    }


async def _confirmar_rascunho(conn, uid_cuidador, patient_id, caregiver_name=""):
    """Grava o rascunho na agenda apos confirmacao (voz ou botao)."""
    draft = obter_rascunho(uid_cuidador, patient_id)
    if not draft:
        return {"fala": "Não tenho nenhum agendamento pendente para confirmar. "
                        "Pode pedir, por exemplo: “Agenda uma consulta com o "
                        "cardiologista quinta às 14h”?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    from api_identity import require_family_access
    await require_family_access(conn, uid_cuidador, patient_id)

    try:
        quando = datetime.fromisoformat(draft["scheduledDate"])
    except Exception:
        descartar_rascunho(uid_cuidador, patient_id)
        return {"fala": "Não consegui ler a data do agendamento. Vamos tentar de novo?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    consulta_id = uuid.uuid4()
    row = await conn.fetchrow(
        """INSERT INTO consultations
           (id, user_id, title, scheduled_date, status, created_at,
            caregiver_id, caregiver_name)
           VALUES ($1, $2, $3, $4, 'ACCEPTED', now(), $5, $6)
           RETURNING id, title, scheduled_date""",
        consulta_id, patient_id, draft["title"], quando, uid_cuidador,
        draft.get("caregiver_name") or caregiver_name or "Cuidador")

    descartar_rascunho(uid_cuidador, patient_id)

    data_pt = row["scheduled_date"].strftime("%d/%m/%Y às %Hh%M")
    return {
        "fala": f"Confirmado! Consulta com {row['title']} no dia {data_pt}. "
                "Já está na agenda do aplicativo.",
        "acao": "CONSULTA_AGENDADA",
        "parametros": {
            "id": str(row["id"]),
            "title": row["title"],
            "scheduledDate": row["scheduled_date"].isoformat(),
        },
    }


async def confirmar_rascunho_por_id(conn, uid_cuidador: str, patient_id: str,
                                     draft_id: str, caregiver_name: str = "") -> dict:
    """Confirmacao via BOTAO na tela (chamada REST, nao por voz).

    Valida que o draft existe e pertence ao par cuidador/paciente — um
    identificador forjado nao agenda nada (fail-closed).
    """
    draft = obter_rascunho(uid_cuidador, patient_id)
    if not draft or draft.get("id") != draft_id:
        return {"fala": "Esse agendamento expirou ou não é seu. Vamos refazer?",
                "acao": "AGENDAR_FALHA", "parametros": {}}
    return await _confirmar_rascunho(conn, uid_cuidador, patient_id, caregiver_name)
