"""Roteador determinístico do ORB (Fase 2).

Camada 0 do assistente: resolve sem LLM tudo que tem resposta certa
(saudação, valores de saúde com número, tópico clínico, emergência,
lembretes/exames explícitos, fofoca cotidiana). Retorna None apenas
para conversa aberta real — único caso que chega ao Groq.

Regras de segurança do roteador:
- NUNCA inventa número: todo número da resposta foi extraído da fala.
- Chat NUNCA registra: ações clínicas viram CONVERSA com template
  que redireciona ao copiloto de voz (Fase 1).
- ANTI-FANTASMA DE DIETA: sem menção explícita a comida, nada de
  refeição — fofoca cotidiana cai em SMALLTALK (CONVERSA).
"""

import re
import time
import unicodedata

# ------------------------------------------------------------------
# Consultas (PR2, passo 2.3): intenção de VER um dado, não de registrar.
# Só vale quando a frase não traz número — número é registro (passo 2.4) —
# e há um marcador de pergunta/pedido de informação.
# ------------------------------------------------------------------
_QUERY_MARKERS = re.compile(
    r"\?|^(qual|quais|como|quando|quanto|quanta|quantos|quantas|onde|o que|"
    r"me diz|me fale|me fala|mostra|mostre|ver|consulte|consultar|consulta|"
    r"ultima|ultimo|ultimos|ultimas|resumo|hoje)\b"
)

_QUERY_METRICAS = (
    ("query_bp", r"\bpressao\b"),
    ("query_glucose", r"\bglicemi|\bglicose\b"),
    ("query_hr", r"\bbatimento|\bfrequencia cardiaca"),
    ("query_spo2", r"\bsaturacao|\boxigenacao|\boxigenio"),
    ("query_weight", r"\bpeso\b"),
    ("query_water", r"\bagua\b|\bhidratacao|\bhidrata"),
    ("query_summary", r"\bresumo\b|\bcomo (eu )?(estou|to)\b|\bmeu dia\b|\bcomo ando\b|\bcomo (esta|vai|ta)\b"),
)

# Menção genérica a dados de saúde sem métrica específica: ambíguo.
_QUERY_AMBIGUOUS = re.compile(
    r"\bmeus (dados|registros|metricas|resultados|exames)\b|"
    r"\bminhas (metricas|medicoes|leituras)\b|\bmeu historico\b"
)

QUERY_KINDS = tuple(k for k, _ in _QUERY_METRICAS) + ("query_ambiguous",)

QUERY_AMBIGUOUS_FALA = (
    "Posso consultar várias coisas suas: última pressão, glicemia, batimentos, "
    "saturação, peso, água de hoje ou o resumo do dia. Qual você quer?"
)


def detect_query(norm: str):
    """Kind da consulta (query_*) ou None. Número na frase é registro, não consulta."""
    if not norm or re.search(r"\d", norm):
        return None
    if not _QUERY_MARKERS.search(norm):
        return None
    for kind, padrao in _QUERY_METRICAS:
        if re.search(padrao, norm):
            return kind
    if _QUERY_AMBIGUOUS.search(norm):
        return "query_ambiguous"
    return None


# ------------------------------------------------------------------
# Tomada de medicação (PR3, passo 3.3): intenção de marcar dose tomada.
# ------------------------------------------------------------------
# Só conta como tomada a forma no passado ou no imperativo. O infinitivo
# ("vou tomar", "preciso tomar", "me lembra de tomar") é intenção ou pedido
# de lembrete — nunca registra dose (regra do passo 3.2/3.3: sem dados
# suficientes, não há ocorrência).
_TAKE_VERBS = re.compile(
    r"\btomei|\btomou|\btomado|\btomados|\btomada|\btomadas|\btomando|"
    r"\btoma\b|\btome\b|\bacabei de tomar|\bacabo de tomar")
_MED_CONTEXT = re.compile(
    r"\bremedio|\bremedios|\bmedicamento|\bmedicamentos|\bcomprimido|\bcapsula|"
    r"\bxarope|\bgota|\bgotas|\binjecao|\bpedra")


def detect_med_take(norm: str) -> bool:
    """True quando a fala é marcação de dose tomada.

    Exige verbo de tomar (passado/imperativo) E (palavra de remédio ou horário
    explícito). Só nome do remédio, sem horário e sem palavra genérica, não
    engatilha: não dá para saber se é tomada ou conversa sobre o remédio.
    """
    if not _TAKE_VERBS.search(norm):
        return False
    if _MED_CONTEXT.search(norm):
        return True
    from services.medication_lookup import detect_hour
    if detect_hour(norm) is not None:
        return True
    return bool(re.search(r"\b(?:tomei|tomou|acabei de tomar)\b", norm)
                and not re.search(r"\b(?:agua|cafe|cha|suco|leite|cerveja|banho|copo|xicara|caneca)\b", norm))


# ------------------------------------------------------------------
# Normalização
# ------------------------------------------------------------------

def normalize(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados."""
    unicoded_text = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in unicoded_text if unicodedata.category(c) != "Mn")
    no_acc = no_acc.lower()
    no_acc = re.sub(r"\s+", " ", no_acc).strip()
    return no_acc


_NUMBER_WORDS = {
    "zero": "0", "um": "1", "uma": "1", "dois": "2", "duas": "2",
    "tres": "3", "quatro": "4", "cinco": "5", "seis": "6", "sete": "7",
    "oito": "8", "nove": "9", "dez": "10", "onze": "11", "doze": "12",
    "treze": "13", "catorze": "14", "quatorze": "14", "quinze": "15",
    "dezesseis": "16", "dezessete": "17", "dezoito": "18", "dezenove": "19",
    "vinte": "20", "trinta": "30", "quarenta": "40", "cinquenta": "50",
    "sessenta": "60", "setenta": "70", "oitenta": "80", "noventa": "90",
    "cem": "100", "cento": "100", "duzentos": "200", "ml": "ml",
}


def words_to_digits(text: str) -> str:
    """Troca números por extenso por dígitos (palavra inteira)."""
    for word, digit in _NUMBER_WORDS.items():
        text = re.sub(r"\b%s\b" % word, digit, text)
    return text


def _conversa(fala: str) -> dict:
    return {"fala": fala, "acao": "CONVERSA", "parametros": {}}


# ------------------------------------------------------------------
# Faixas clínicas (SBC / SBD) — só leitura, sem diagnóstico
# ------------------------------------------------------------------

def classify_bp(sys: int, dia: int) -> str:
    if sys >= 180 or dia >= 110:
        return "no estágio 3 de hipertensão (faixa de atenção imediata)"
    if sys >= 160 or dia >= 100:
        return "no estágio 2 de hipertensão"
    if sys >= 140 or dia >= 90:
        return "no estágio 1 de hipertensão"
    if sys >= 130 or dia >= 85:
        return "na faixa de pré-hipertensão"
    if sys >= 120 or dia >= 80:
        return "na faixa normal"
    return "na faixa ótima (abaixo de 120 por 80)"


def classify_glucose(val: int) -> str:
    if val < 70:
        return "na faixa de hipoglicemia (abaixo de 70 mg/dL, requer atenção)"
    if val <= 99:
        return "na faixa normal de jejum (70 a 99 mg/dL)"
    if val <= 125:
        return "na faixa de pré-diabetes em jejum (100 a 125 mg/dL)"
    return "em faixa elevada em jejum (a partir de 126 mg/dL)"


# ------------------------------------------------------------------
# Detectores (todos sobre texto normalizado + dígitos)
# ------------------------------------------------------------------

_BP_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d+)?)\s*(?:por|/|\bx\b|-)\s*(\d{1,3}(?:[.,]\d+)?)"
    r"(?:\s*e\s*(meio|meia|\d+))?"
)

_EMERGENCY_WORDS = (
    "dor no peito", "falta de ar", "desmaio", "desmai", "samu", "192",
    "derrame", "infarto", "avc", "convuls",
)

_GLUCOSE_WORDS = ("glicemia", "glicose", "acucar no sangue", "diabete")
_HR_WORDS = ("batimento", "frequencia cardiaca", "pulso", "bpm")
_SPO2_WORDS = ("spo2", "spo 2", "saturacao", "oxigenacao", "oximetro")
_HYDR_WORDS = ("agua", "h2o", "hidrata", "copo", "garrafa", "garrafinha",
               "xicara", "caneca", "litro", "ml")
_MED_WORDS = ("remedio", "medicamento", "comprimido")
_MEAL_VERBS = ("comi", "almocei", "jantei", "lanchei", "tomei cafe",
               "acabei de comer", "bebi")
_NUTRI_WORDS = ("dieta", "alimentacao", "nutri", "caloria", "o que comer",
                "posso comer", "devo comer", "posso almocar", "posso jantar",
                "sugestao de", "recomenda")
_TOPIC_RE = re.compile(
    r"\b(pressao|glicemia|glicose|diabetes|colesterol|dor|dores|febre|"
    r"sintoma|sintomas|tontura|enjoo|enjoos|remedio|medicamento|insonia|ansiedade|"
    r"diarreia|diarreias|vomit\w*|nausea|nauseas|coco|cagando|fezes|"
    r"constipacao|azia|refluxo|dor de cabeca|dor de barriga)\b"
)
_BOT_RE = re.compile(
    r"\b(voce tem|voce sente|voce esta|e voce|e vc|voce sofre|voce passa mal)\b"
)
_TIME_RE = re.compile(r"\b(que\s+horas\s+s[aã]o|que\s+horas\s+tem|me\s+diz\s+as\s+horas|qual\s+a\s+hora|horas\s+s[aã]o|que\s+horas)\b")
_DATE_RE = re.compile(r"\b(que\s+dia\s+[eé]\s+hoje|que\s+dia\s+[eé]|qual\s+a\s+data|data\s+de\s+hoje|qual\s+o\s+dia|que\s+dia\s+da\s+semana)\b")
_SYMPTOM_GI_RE = re.compile(
    r"\b(diarreia|diarreias|vomit\w*|nausea|nauseas|coco|cagando|fezes|"
    r"constipacao|azia|refluxo|dor de barriga)\b"
)
_SYMPTOM_HEADACHE_RE = re.compile(
    r"\b(dor de cabeca|cefaleia|enxaqueca)\b"
)
_SYMPTOM_FEVER_RE = re.compile(
    r"\b(febre|febril|calafrio|calafrios)\b"
)
SYMPTOM_KINDS = ("symptom_gi", "symptom_headache", "symptom_fever")
_QUESTION_STARTS = ("como", "qual", "quais", "quando", "onde", "por que",
                    "porque", "o que", "voce", "é normal", "e normal",
                    "me explica", "me conta", "quanto", "quantos")


def _to_int(num: str):
    try:
        return int(float(num.replace(",", ".")))
    except (ValueError, TypeError):
        return None


def detect_emergency(norm: str) -> bool:
    return any(w in norm for w in _EMERGENCY_WORDS)


def detect_bp(norm: str):
    m = _BP_RE.search(norm)
    if not m:
        return None
    sys = _to_int(m.group(1))
    dia = _to_int(m.group(2))
    if sys is None or dia is None:
        return None
    if sys < 40:
        sys *= 10
    if dia < 40:
        dia *= 10
    half = (m.group(3) or "").strip()
    if half in ("meio", "meia"):
        dia += 5
    elif half.isdigit():
        dia += int(half)
    if not (60 <= sys <= 280 and 30 <= dia <= 180 and sys > dia):
        return None
    return sys, dia


def detect_glucose(norm: str):
    if not any(w in norm for w in _GLUCOSE_WORDS):
        return None
    for m in re.finditer(r"(\d{2,3})", norm):
        val = int(m.group(1))
        if 20 <= val <= 600:
            return val
    return None


def detect_hr(norm: str):
    if not any(w in norm for w in _HR_WORDS):
        return None
    for m in re.finditer(r"(\d{2,3})", norm):
        val = int(m.group(1))
        if 30 <= val <= 250:
            return val
    return None


def detect_spo2(norm: str):
    if not any(w in norm for w in _SPO2_WORDS):
        return None
    for m in re.finditer(r"(\d{2,3})", norm):
        val = int(m.group(1))
        if 50 <= val <= 100:
            return val
    return None


def detect_hydration(norm: str):
    if not any(w in norm for w in _HYDR_WORDS):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|mililitro|litro|l|copo|garrafinha|garrafa|xicara|caneca)", norm)
    if m:
        amount = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        per = {"ml": 1, "mililitro": 1, "litro": 1000, "l": 1000,
               "copo": 250, "garrafinha": 500, "garrafa": 1000,
               "xicara": 200, "caneca": 350}.get(unit, 250)
        total = int(amount * per)
        if 50 <= total <= 8000:
            return total
        return None
    if "agua" in norm or "h2o" in norm or "hidrata" in norm:
        return 250
    return None


# ------------------------------------------------------------------
# Peso (PR2, passo 2.4): reconhecimento deterministico com faixa clinica.
# ------------------------------------------------------------------
_WEIGHT_CONTEXT = re.compile(r"\bpeso\b|\bpesando\b|\bquilos?\b")
_WEIGHT_NUM = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)")


def detect_weight(norm: str):
    """Peso em kg (20-300), ou None. Exige contexto de peso + número."""
    if not _WEIGHT_CONTEXT.search(norm):
        return None
    m = _WEIGHT_NUM.search(norm)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    # Faixa do HEALTH_VALIDATION_RULES do core/confirmation (PESO).
    if not (20 <= val <= 300):
        return None
    return val


# ------------------------------------------------------------------
# Roteador principal (Fase 2) + API estruturada (Fase 4)
# ------------------------------------------------------------------

def _period_greeting() -> str:
    h = time.localtime().tm_hour
    if 5 <= h < 12:
        return "Bom dia! Tudo bem com você? Como posso te ajudar hoje?"
    if 12 <= h < 18:
        return "Boa tarde! Tudo bem por aí? Como posso te ajudar hoje?"
    return "Boa noite! Espero que seu dia tenha sido tranquilo. Como posso te ajudar?"


def detect_all(raw: str) -> dict:
    """Classificação estruturada sem LLM.

    Retorna {'kind': ...} + dados (sys/dia, valor, ml...). Kinds:
    empty, emergency, bp, glucose, hydration, hr, spo2, query_* (consulta
    de métrica), reminder, exams, meds, nutrition, greeting_*,
    gratitude, app, identity, topic, smalltalk, open.
    """
    if not raw or not raw.strip():
        return {"kind": "empty"}
    norm = words_to_digits(normalize(raw))

    if detect_emergency(norm):
        return {"kind": "emergency"}
    # AUD-G17: a detecção de PA (X por Y) vinia ANTES da de data. "Que dia é
    # hoje, 20/09/2026?" casava o padrão "20/09" como pressão 200x90 e o
    # usuário ouvia um absurdo clínico. Perguntas de data/hora são resolvidas
    # primeiro — elas nunca contêm um valor de saúde.
    if _TIME_RE.search(norm):
        return {"kind": "time"}
    if _DATE_RE.search(norm):
        return {"kind": "date"}
    if re.search(r"\blembrete|\bme lembra|\bme avisa", norm):
        return {"kind": "reminder"}
    # Passo 3.3: tomada de dose é reconhecida ANTES da consulta — "tomei o
    # remédio da pressão das 8" carrega a palavra da métrica, mas é registro,
    # não pergunta. Sem verbo de tomada, a consulta segue valendo.
    if detect_med_take(norm):
        return {"kind": "med_take"}
    # Passo 2.3: consulta (pergunta sem valor) é respondida pelo serviço de
    # consultas, antes de qualquer conversa aberta.
    query = detect_query(norm)
    if query:
        return {"kind": query}
    bp = detect_bp(norm)
    if bp:
        return {"kind": "bp", "sys": bp[0], "dia": bp[1]}
    glu = detect_glucose(norm)
    if glu:
        return {"kind": "glucose", "valor": glu}
    hyd = detect_hydration(norm)
    if hyd:
        return {"kind": "hydration", "ml": hyd}
    hr = detect_hr(norm)
    if hr:
        return {"kind": "hr", "bpm": hr}
    spo2 = detect_spo2(norm)
    if spo2:
        return {"kind": "spo2", "valor": spo2}
    peso = detect_weight(norm)
    if peso:
        return {"kind": "weight", "peso_kg": peso}
    if re.search(r"\bexame", norm):
        return {"kind": "exams"}
    if any(w in norm for w in _MED_WORDS):
        return {"kind": "meds"}
    if any(v in norm for v in _MEAL_VERBS) or any(w in norm for w in _NUTRI_WORDS):
        return {"kind": "nutrition"}
    try:
        from orb_actions import extract_grocery_items
    except Exception:
        extract_grocery_items = None
    if extract_grocery_items is not None and re.search(
            r"\b(lista|compr\w*|mercado|feira|adiciona|adicione|anota|anote)\b", norm):
        return {"kind": "grocery", "items": extract_grocery_items(norm)}
    # AUD-G17: data/hora agora é resolvido ANTES da PA (movido para cima).
    if re.search(r"\bbom dia\b|\botimo dia\b", norm):
        return {"kind": "greeting_morning"}
    if re.search(r"\bboa tarde\b|\botima tarde\b", norm):
        return {"kind": "greeting_afternoon"}
    if re.search(r"\bboa noite\b|\bvou dormir\b|\bate amanha\b", norm):
        return {"kind": "greeting_evening"}
    if re.search(r"\b(oi|ola|opa|e ai)\b|\btudo bem\b|\bcomo vai\b", norm):
        return {"kind": "greeting"}
    if re.search(r"\bobrigad|\bvaleu\b|\bagradec|\bgratid", norm):
        return {"kind": "gratitude"}
    if re.search(r"\bplay store\b|\blink do app\b|\blink do aplicativo\b|\bbaixar o app\b|\bonde baixo\b|\binstalar o app\b|\bdownload do app\b", norm):
        return {"kind": "app"}
    if re.search(r"qual (e|o) seu nome|quem e voce|voce e quem|o que voce (faz|sabe|pode)|o que voce consegue|capaz de fazer|do que voce e capaz", norm):
        return {"kind": "identity"}
    if _BOT_RE.search(norm):
        return {"kind": "bot"}
    m_gi = _SYMPTOM_GI_RE.search(norm)
    if m_gi:
        return {"kind": "symptom_gi"}
    if _SYMPTOM_HEADACHE_RE.search(norm):
        return {"kind": "symptom_headache"}
    if _SYMPTOM_FEVER_RE.search(norm):
        return {"kind": "symptom_fever"}
    if _TOPIC_RE.search(norm):
        return {"kind": "topic"}
    words = norm.split()
    is_question = "?" in raw or norm.startswith(_QUESTION_STARTS)
    if not is_question and len(words) <= 15:
        return {"kind": "smalltalk"}
    return {"kind": "open"}


def symptom_fala(kind: str) -> str:
    """Orientação geral por domínio de sintoma (sem diagnóstico, sem remédio)."""
    if kind == "symptom_gi":
        return ("Entendi. Na diarreia, o mais importante é não desidratar: beba água e soro caseiro aos poucos. "
                "Se tiver sangue nas fezes, febre alta ou durar mais de dois dias, procure uma UBS ou UPA.")
    if kind == "symptom_headache":
        return ("Entendi, dor de cabeça é muito incômoda. Descanse em local calmo, beba água e evite se automedicar. "
                "Se for súbita e muito forte, com febre ou rigidez na nuca, procure emergência.")
    if kind == "symptom_fever":
        return ("Entendi, febre exige atenção. Mantenha-se hidratado e em repouso, e acompanhe a temperatura. "
                "Se for alta ou durar mais de dois dias, procure uma UBS ou UPA.")
    return ""


ESCALATION_SUFFIX = (" Reforçando o essencial: anote o sintoma, quando começou e se tem febre para mostrar ao médico, "
                     "e procure uma UBS ou UPA se piorar.")


def symptom_repeat(memory: dict, key: str, kind: str, ttl: int = 600, now: float = None) -> int:
    """Conta quantas vezes seguidas o mesmo sintoma apareceu (TTL 10 min)."""
    ts = time.time() if now is None else now
    if len(memory) > MEMORY_MAX_KEYS:
        memory.clear()
    hit = memory.get(key)
    if hit and ts - hit[1] <= ttl and hit[0] == kind:
        n = hit[2] + 1
    else:
        n = 1
    memory[key] = (kind, ts, n)
    return n


def route_chat(raw: str):
    """Roteia a mensagem do chat.

    Retorna dict {fala, acao, parametros} pronto para o cliente,
    ou None quando é conversa aberta de verdade (caso p/ o LLM).
    Saudações, agradecimentos e conversas cotidianas passam para o LLM
    para manter um diálogo caloroso, humano e espontâneo.
    """
    norm = normalize(raw)
    if re.search(r"\b(registr\w*|anot\w*|adicion\w*|coloc\w*)\b", norm) and re.search(
            r"\b(refeicao|refeicoes|alimentacao|almoco|jantar|lanche)\b", norm):
        return _conversa("Para registrar sua refeição, use a tela de Alimentação do aplicativo.")
    det = detect_all(raw)
    kind = det["kind"]
    if kind in ("empty", "open", "greeting_morning", "greeting_afternoon", "greeting_evening", "greeting", "gratitude", "smalltalk"):
        return None
    if kind == "emergency":
        return {
            "fala": "Isso parece urgente. Acione o SAMU 192 ou vá ao pronto atendimento agora. Já deixei as opções de socorro na sua tela.",
            "acao": "EMERGENCIA",
            "parametros": {},
        }
    if kind == "time":
        import time as t_mod
        lt = t_mod.localtime()
        h = lt.tm_hour
        m = lt.tm_min
        time_str = f"Agora são {h} horas." if m == 0 else f"Agora são {h} horas e {m} minutos."
        return _conversa(time_str)
    if kind == "date":
        import datetime
        now = datetime.datetime.now()
        dias = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        meses = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
        dia_semana = dias[now.weekday()]
        mes = meses[now.month]
        date_str = f"Hoje é {dia_semana}, {now.day} de {mes} de {now.year}."
        return _conversa(date_str)
    if kind == "bp":
        band = classify_bp(det["sys"], det["dia"])
        return _conversa(
            f"Entendi: {det['sys']} por {det['dia']}. De acordo com a Sociedade Brasileira de Cardiologia, esse valor está {band}. "
            "Você pode anotar na tela de pressão do aplicativo, e se preferir é só chamar o assistente de voz que ele preenche rapidinho com você."
        )
    if kind == "glucose":
        band = classify_glucose(det["valor"])
        return _conversa(
            f"Entendi: {det['valor']} de glicemia. De acordo com a Sociedade Brasileira de Diabetes, esse valor está {band}. "
            "Você pode anotar na tela de glicemia do aplicativo, e se preferir é só chamar o assistente de voz que ele preenche rapidinho com você."
        )
    if kind == "hydration":
        return _conversa(
            f"Boa! {det['ml']} ml de água contam. A orientação geral é cerca de 2 litros por dia. "
            "Você pode anotar na tela de água do aplicativo, e se preferir o assistente de voz registra com você."
        )
    if kind == "hr":
        return _conversa(
            f"Entendi: {det['bpm']} batimentos por minuto. Você pode anotar na tela de batimentos do aplicativo, "
            "e se preferir é só chamar o assistente de voz para registrar com facilidade."
        )
    if kind == "spo2":
        return _conversa(
            f"Entendi: {det['valor']} por cento de saturação. Você pode anotar na tela de oxigenação do aplicativo, "
            "e se preferir é só chamar o assistente de voz para registrar com facilidade."
        )
    if kind == "reminder":
        return {"fala": "Abrindo seus lembretes de remédios.", "acao": "LEMBRETES", "parametros": {}}
    if kind == "exams":
        return {"fala": "Abrindo o organizador local de exames.", "acao": "EXAMES", "parametros": {}}
    if kind == "meds":
        return _conversa(
            "Entendi. Eu não receito nem ajusto remédio, isso é com seu médico. "
            "Você pode anotar a tomada nos lembretes do aplicativo, e se preferir o assistente de voz te ajuda a preencher."
        )
    if kind == "nutrition":
        if re.search(r"\b(registr\w*|anot\w*|adicion\w*|coloc\w*)\b", normalize(raw)):
            return _conversa("Para registrar sua refeição, use a tela de Alimentação do aplicativo.")
        return None
    if kind == "grocery":
        items = det.get("items", [])
        if items:
            shown = ", ".join(items)
            return {"fala": f"Preparei sua lista com {shown}. Toque em confirmar para abrir.",
                    "acao": "ABRIR_LISTA_COMPRAS", "parametros": {"items": items}}
        return {"fala": "Abri sua lista de compras. Toque em confirmar.",
                "acao": "ABRIR_LISTA_COMPRAS", "parametros": {"items": []}}
    if kind == "app":
        return _conversa(
            "O aplicativo ainda está em desenvolvimento e em breve sai na Play Store. "
            "Enquanto isso, vale a pena olhar os Termos de Uso e a Política de Privacidade no site do Braga Saúde."
        )
    if kind == "identity":
        return _conversa(
            "Eu sou o Braga, assistente virtual de rotina e autocuidado do Braga Saúde. Posso conversar e ajudar com as funções disponíveis no app."
        )
    if kind == "bot":
        return _conversa(
            "Sou um assistente virtual. Não tenho experiências ou sentimentos próprios, mas posso conversar e ajudar com sua rotina."
        )
    if kind in SYMPTOM_KINDS:
        return _conversa(symptom_fala(kind))
    # topic, smalltalk e qualquer kind desconhecido → LLM responde naturalmente
    return None


# ------------------------------------------------------------------
# Memória curta de clarificação (Fase 4) — sem banco, TTL em memória
# ------------------------------------------------------------------

MEMORY_TTL_S = 600
MEMORY_MAX_KEYS = 2000


def remember(memory: dict, key: str, kind: str, now: float = None) -> None:
    """Guarda a última intenção clarificável (só 'nutrition' hoje)."""
    ts = time.time() if now is None else now
    if len(memory) > MEMORY_MAX_KEYS:
        for k in [k for k, v in memory.items() if ts - v[1] > MEMORY_TTL_S]:
            del memory[k]
        if len(memory) > MEMORY_MAX_KEYS:
            memory.clear()
    memory[key] = (kind, ts)


def recall(memory: dict, key: str, ttl: int = MEMORY_TTL_S, now: float = None):
    """Devolve o kind lembrado ou None (expirado/ausente)."""
    ts = time.time() if now is None else now
    hit = memory.get(key)
    if not hit:
        return None
    kind, saved = hit
    if ts - saved > ttl:
        memory.pop(key, None)
        return None
    return kind


_ATE_RE = re.compile(r"\b(ja comi|comi sim|isso mesmo|foi isso|acabei de comer|sim,? comi|anota\w*|item|itens)\b")
_GROCERY_RE = re.compile(r"\b(lista|compra|comprar|mercado|feira)\b")

_FOLLOWUP_CHAT = {
    "ate": ("Que bom! Você pode registrar na tela de alimentação do aplicativo, "
            "o registro por voz ainda não está disponível para esses itens."),
    "grocery": ("Combinado! Você pode montar a lista na tela de alimentação do aplicativo, "
                "o registro por voz ainda não está disponível para esses itens."),
}
_FOLLOWUP_WA = {
    "ate": ("Que bom! Me conta o que foi que eu te oriento com carinho. "
            "E se quiser registrar a refeição, me fala os itens por aqui mesmo."),
    "grocery": ("Combinado! Você pode anotar os itens na lista de compras do aplicativo, na tela de alimentação."),
}


def resolve_followup(raw: str, last_kind, channel: str = "chat"):
    """Resposta de follow-up quando a msg anterior pediu clarificação.

    last_kind: kind lembrado (só 'nutrition' é clarificável).
    Retorna envelope CONVERSA ou None.
    """
    if last_kind != "nutrition":
        return None
    if not raw or not raw.strip():
        return None
    norm = words_to_digits(normalize(raw))
    table = _FOLLOWUP_WA if channel == "wa" else _FOLLOWUP_CHAT
    if _GROCERY_RE.search(norm):
        return _conversa(table["grocery"])
    if _ATE_RE.search(norm):
        return _conversa(table["ate"])
    return None


# ------------------------------------------------------------------
# Anti-repetição de template (nunca mandar a mesma fala 2x seguidas)
# ------------------------------------------------------------------

LAST_FALA_MAX_KEYS = 2000
_REPEAT_SUFFIX = (" Se quiser, te explico de outro jeito ou te mostro "
                  "onde anotar no aplicativo. É só pedir!")


def dedupe_fala(memory: dict, key: str, fala: str) -> str:
    """Devolve a fala, variando se for idêntica à última enviada."""
    if len(memory) > LAST_FALA_MAX_KEYS:
        memory.clear()
    if memory.get(key) == fala:
        return fala + _REPEAT_SUFFIX
    memory[key] = fala
    return fala


# ------------------------------------------------------------------
# Mapeamento WhatsApp (Fase 4) — sem "voz do aplicativo" por lá
# ------------------------------------------------------------------

_SAMU_FALA = ("Isso parece urgente. Acione o SAMU 192 ou vá à UPA/pronto-socorro "
              "mais próximo agora.")

_WA_VITALS = {
    "bp": ("PRESSAO", lambda d: {"sistolica": d["sys"], "diastolica": d["dia"]}),
    "glucose": ("GLICEMIA", lambda d: {"glicemia": d["valor"]}),
    "hydration": ("AGUA", lambda d: {"quantidade_ml": d["ml"]}),
    "hr": ("BATIMENTOS", lambda d: {"batimentos": d["bpm"]}),
    "spo2": ("OXIGENACAO", lambda d: {"saturacao": d["valor"]}),
    # Passo 2.4: peso vai para a biometria do app, não para vital_signs.
    "weight": ("PESO", lambda d: {"peso_kg": d["peso_kg"]}),
}


def whatsapp_next(raw: str, is_registered: bool) -> dict:
    """Decide o WhatsApp antes do LLM.

    Retorna {'type': 'llm'} (segue fluxo atual),
    {'type': 'text', 'fala': ...} (resposta direta, sem provedor) ou
    {'type': 'vitals', 'metric': ..., 'values': ..., 'fala': ...}
    (vai direto aos botões de confirmação, sem provedor).
    """
    det = detect_all(raw)
    kind = det["kind"]
    if kind in ("empty", "open", "smalltalk",
                 "greeting_morning", "greeting_afternoon", "greeting_evening",
                 "greeting", "gratitude"):
        return {"type": "llm"}
    if kind == "emergency":
        return {"type": "text", "fala": _SAMU_FALA}
    if kind in _WA_VITALS:
        metric, to_values = _WA_VITALS[kind]
        values = to_values(det)
        if is_registered:
            if kind == "bp":
                summary = f"{det['sys']} por {det['dia']}"
            elif kind == "glucose":
                summary = f"{det['valor']} de glicemia"
            elif kind == "hydration":
                summary = f"{det['ml']} ml de água"
            elif kind == "hr":
                summary = f"{det['bpm']} bpm"
            elif kind == "weight":
                summary = f"{det['peso_kg']} kg de peso"
            else:
                summary = f"{det['valor']}% de saturação"
            return {"type": "vitals", "metric": metric, "values": values,
                    "fala": f"[{metric}] {summary} via router."}
        if kind == "bp":
            band = classify_bp(det["sys"], det["dia"])
            fala = (f"Entendi: {det['sys']} por {det['dia']}. De acordo com a Sociedade Brasileira "
                    f"de Cardiologia, esse valor está {band}. Para acompanhar e gerar seu relatório, "
                    "vale conhecer o aplicativo Braga Saúde. E qualquer dúvida sobre o valor, fale com seu médico.")
        elif kind == "glucose":
            band = classify_glucose(det["valor"])
            fala = (f"Entendi: {det['valor']} de glicemia. De acordo com a Sociedade Brasileira "
                    f"de Diabetes, esse valor está {band}. Para acompanhar e gerar seu relatório, "
                    "vale conhecer o aplicativo Braga Saúde. E qualquer dúvida sobre o valor, fale com seu médico.")
        elif kind == "hydration":
            fala = (f"Boa! {det['ml']} ml de água contam. A orientação geral é cerca de 2 litros por dia. "
                    "Para acompanhar sua hidratação, vale conhecer o aplicativo Braga Saúde.")
        elif kind == "hr":
            fala = (f"Entendi: {det['bpm']} batimentos por minuto. Se sentir algo estranho, procure atendimento. "
                    "Para acompanhar, vale conhecer o aplicativo Braga Saúde.")
        else:
            fala = (f"Entendi: {det['valor']} por cento de saturação. Se sentir falta de ar, procure atendimento. "
                    "Para acompanhar, vale conhecer o aplicativo Braga Saúde.")
        return {"type": "text", "fala": fala}
    if kind == "reminder":
        return {"type": "text",
                "fala": "Lembretes de remédio ficam no aplicativo, na tela de lembretes. Quer que eu te explique como ativar?"}
    if kind == "exams":
        return {"type": "text",
                "fala": "No aplicativo, Organizar exames prepara um PDF para a consulta. A sessão é temporária: salve o PDF fora do app. Os documentos não são enviados ao servidor."}
    if kind == "meds":
        return {"type": "text",
                "fala": "Entendi. Eu não receito nem ajusto remédio, isso é com seu médico. Se quiser ajuda para organizar os horários, me fala quais são."}
    if kind == "nutrition":
        return {"type": "text",
                "fala": "Sobre alimentação posso orientar de forma geral, sem inventar nada do que você comeu. Me conta o que foi que eu te explico com carinho."}
    if kind == "grocery":
        # WhatsApp não tem executor de lista (contrato B0): orienta, não promete.
        return {"type": "text",
                "fala": "Combinado! Você pode anotar os itens na lista de compras do aplicativo, na tela de alimentação."}
    if kind == "app":
        return {"type": "text",
                "fala": "O aplicativo ainda está em desenvolvimento e em breve sai na Play Store. Enquanto isso, vale a pena olhar os Termos de Uso e a Política de Privacidade no site do Braga Saúde."}
    if kind == "identity":
        return {"type": "text",
                "fala": "Eu sou o Braga, seu assistente de saúde e rotina. Acompanho seus sinais, água e refeições e oriento seu autocuidado."}
    if kind == "bot":
        return {"type": "text",
                "fala": "Sou um assistente virtual. Não tenho experiências ou sentimentos próprios, mas posso conversar e ajudar com sua rotina."}
    if kind in SYMPTOM_KINDS:
        return {"type": "text", "fala": symptom_fala(kind)}
    # topic e qualquer kind não tratado → LLM responde naturalmente
    return {"type": "llm"}
