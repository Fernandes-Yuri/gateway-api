"""Agendamento de consulta por voz no MODO CUIDADOR (D50).

O cuidador fala com o assistente: "Agenda uma consulta com o cardiologista
quinta-feira às 14h". Este modulo:

  1. detecta a intencao de agendar (deterministico, regex);
  2. extrai tipo de consulta, data e hora da fala;
  3. grava na tabela consultations (user_id = paciente, caregiver_id = o
     cuidador autenticado, status = ACCEPTED — o cuidador ja confirmou).

Nao usa LLM: datas/horas por voz sao estruturadas e regex evita alucinacao de
agenda. Se nao conseguir extrair a data, o assistente pede esclarecimento em
vez de gravar nada (fail-closed).
"""
import re
from datetime import datetime, timedelta, timezone

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
        agora = datetime.now(timezone.utc)

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
                                mm if mm >= 0 else 0, tzinfo=timezone.utc)
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
    """Grava a consulta e retorna o envelope {fala, acao, parametros}.

    Fail-closed: retorna acao AGENDAR_FALHA sem gravar nada se a data/hora
    nao puder ser extraida com seguranca.
    """
    from api_identity import require_family_access
    await require_family_access(conn, uid_cuidador, patient_id)

    if not quer_agendar(texto_falado):
        return {"fala": "Não entendi um agendamento. Pode repetir, por exemplo: "
                        "“Agenda uma consulta com o cardiologista quinta às 14h”?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    titulo = extrair_especialidade(texto_falado)
    quando, quando_str = extrair_data_hora(texto_falado)

    if quando is None:
        return {"fala": f"Para agendar a consulta com {titulo.lower()}, preciso da data e "
                        "da hora. Pode dizer, por exemplo: “quinta-feira às 14h”?",
                "acao": "AGENDAR_FALHA", "parametros": {"titulo": titulo}}

    if quando < datetime.now(timezone.utc):
        return {"fala": "Essa data já passou. Quer agendar para outra data?",
                "acao": "AGENDAR_FALHA", "parametros": {}}

    import uuid
    consulta_id = uuid.uuid4()
    row = await conn.fetchrow(
        """INSERT INTO consultations
           (id, user_id, title, scheduled_date, status, created_at,
            caregiver_id, caregiver_name)
           VALUES ($1, $2, $3, $4, 'ACCEPTED', now(), $5, $6)
           RETURNING id, title, scheduled_date""",
        consulta_id, patient_id, titulo, quando, uid_cuidador,
        caregiver_name or "Cuidador")

    data_pt = quando.strftime("%d/%m/%Y às %Hh%M")
    return {
        "fala": f"Consulta com {titulo} agendada para {data_pt}. Já está na agenda "
                "do aplicativo e o paciente será notificado.",
        "acao": "CONSULTA_AGENDADA",
        "parametros": {
            "id": str(row["id"]),
            "title": row["title"],
            "scheduledDate": row["scheduled_date"].isoformat(),
        },
    }
