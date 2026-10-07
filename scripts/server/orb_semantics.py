"""Significado do pedido atual, independente do provedor e do transporte."""
import re
import unicodedata


def normalize(text):
    return re.sub(r"\s+", " ", "".join(c for c in unicodedata.normalize("NFD", text or "")
                                          if unicodedata.category(c) != "Mn").lower()).strip()


CLAUSES = re.compile(r"\b(?:mas|porem|contudo)\b|(?<!que )\be\b|[;!?]|\.(?!\d)|,\s*(?=(?:eu|ele|ela|meu|minha|nao|estou|tenho|sinto|bebi|tomei|agora)\b)")
DENIAL = re.compile(r"\b(?:nao|sem|nego|negou|nunca|jamais)\b(?:\W+\w+){0,6}\W*$")
EDUCATIONAL = re.compile(r"\b(?:o que (?:e|significa|causa)|por que|porque|como (?:evitar|prevenir)|explique|explica)\b")
HYPOTHETICAL = re.compile(r"\b(?:se (?:eu |ele |ela |alguem )?(?:tiver|sentir)|hipotet\w*|exemplo|no filme|no livro|li a frase)\b")
CURRENT = re.compile(r"\b(?:estou|tenho|sinto|sentindo|agora|socorro|nao (?:consigo|consegue)|cai|caiu|bati a cabeca|levei um tombo|escorreguei)\b|\b(?:meu|minha|ele|ela)\b.{0,40}\b(?:esta|tem|sente)\b")
PAST = re.compile(r"\b(?:ontem|anteontem|semana passada|mes passado|ano passado|ja tive|ja teve|tive|teve|sentia|senti|sentiu|ha \d+ (?:dias|meses|anos))\b")
RECURRENCE = re.compile(r"\b(?:voltou|voltaram|de novo|outra vez|novamente|piorou|piorando|continua|ainda)\b")
EMERGENCY = re.compile(r"\b(?:socorro|dor (?:forte |insuportavel )?no peito|aperto (?:insuportavel )?no peito|suor frio|falta de ar(?: repentina)?|nao (?:consigo|consegue) respirar?|desmai\w*|boca (?:ta |esta )?torta|vomitando sangue|infarto|avc|derrame|convuls\w*|perdi a forca|dormencia|formigamento|ajuda rapido|passando muito mal|tontura (?:muito )?forte|visao escureceu|puxa pro braco|queimacao forte no meio do peito|nao (?:consigo|consegue) (?:me |se )?levantar|bati a cabeca|ta sangrando|esta sangrando|levei um tombo|levou um tombo|escorreguei|cai(?:u)?(?: aqui| no chao)?)\b")
HELP_REQUEST = re.compile(r"\b(?:lig\w*|cham\w*|acion\w*|abr\w*)\b.{0,30}\b(?:samu|192|socorro|emergencia)\b")
DRINK = re.compile(r"\b(?:agua|h2o|suco|cha|cafe|leite|refrigerante|cerveja|vinho|bebida)\b")
INGESTION = re.compile(r"\b(?:bebi|bebeu|tomei|tomou|bebendo|tomando|anot\w*|registr\w*|adicion\w*)\b")
QUANTITY = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mililitros?|ml|litros?|l|copos?|garrafinhas?|garrafas?|xicaras?|canecas?)\b")


def is_current_emergency(raw):
    text = normalize(raw)
    clauses = CLAUSES.split(text)
    for index, clause in enumerate(clauses):
        recurring = bool(RECURRENCE.search(clause))
        resumed = any(re.fullmatch(r"\s*(?:(?:agora|hoje)\s+)?(?:voltou|voltaram|continua|piorou)\s*", later)
                      for later in clauses[index + 1:])
        for match in list(EMERGENCY.finditer(clause)) + list(HELP_REQUEST.finditer(clause)):
            denied = not match.group().startswith("nao ") and DENIAL.search(clause[:match.start()])
            past = PAST.search(clause) and not CURRENT.search(clause) and not recurring and not resumed
            example = HYPOTHETICAL.search(clause) or (EDUCATIONAL.search(clause) and not CURRENT.search(clause) and not recurring)
            if not denied and not past and not example:
                return True
    return False


def beverage_mentions(raw):
    """Não infere água a partir de ml; associa modalidade e entidade por oração."""
    mentions = []
    for clause in CLAUSES.split(normalize(raw)):
        verb = INGESTION.search(clause)
        quantity = QUANTITY.search(clause)
        drinks = list(DRINK.finditer(clause))
        if not verb or not (quantity or drinks):
            continue
        denied = bool(DENIAL.search(clause[:verb.start()]))
        request = bool(re.match(r"(?:anot|registr|adicion)", verb.group()))
        question = bool(EDUCATIONAL.search(clause) or re.search(r"\b(?:quanto|quantos|posso|devo|como)\b", clause))
        ml = None
        if quantity:
            amount = float(quantity[1].replace(",", "."))
            unit = quantity[2].rstrip("s")
            per = {"ml": 1, "mililitro": 1, "litro": 1000, "l": 1000, "copo": 250,
                   "garrafinha": 500, "garrafa": 1000, "xicara": 200, "caneca": 350}[unit]
            total = int(amount * per)
            ml = total if 50 <= total <= 8000 else None
        entity = drinks[0].group() if len(drinks) == 1 else None
        if entity == "h2o":
            entity = "agua"
        mentions.append({"beverage": entity, "quantity_ml": ml, "negated": denied,
                         "modality": "educational" if question else "cancellation" if denied and request
                         else "denial" if denied else "request" if request else "report"})
    return mentions


def beverage_reply(raw):
    if EDUCATIONAL.search(normalize(raw)):
        return None
    mentions = beverage_mentions(raw)
    if not mentions:
        return None
    if len(mentions) != 1:
        return "Você mencionou mais de uma informação sobre bebidas. Qual delas deseja tratar primeiro?"
    mention = mentions[0]
    if mention["modality"] == "cancellation":
        return "Entendi o pedido de não registrar essa bebida. Não vou propor um registro de consumo."
    if mention["negated"]:
        return "Entendi: você negou esse consumo. Não vou considerar essa quantidade como bebida consumida."
    if mention["modality"] == "educational":
        return None
    if not mention["beverage"]:
        return "Qual bebida você está mencionando? A quantidade sozinha não identifica água."
    if mention["beverage"] != "agua":
        shown = "chá" if mention["beverage"] == "cha" else "café" if mention["beverage"] == "cafe" else mention["beverage"]
        amount = f"{mention['quantity_ml']} ml de " if mention["quantity_ml"] else ""
        return f"Você mencionou {amount}{shown}. Não vou converter essa bebida em um registro de água."
    return None


def conversation_only(raw, context):
    # Dado do cliente pode restringir ações, jamais concedê-las.
    return bool(isinstance(context, dict) and context.get("original_text") == raw
                and context.get("conversation_only") is True)


def action_allowed(raw, action, params=None, context=None):
    if action == "CONVERSA":
        return True
    if conversation_only(raw, context):
        return False
    if action == "EMERGENCIA":
        return is_current_emergency(raw)
    text = normalize(raw)
    if re.search(r"\b(?:nao|nunca|jamais|sem)\b", text) or EDUCATIONAL.search(text) or HYPOTHETICAL.search(text):
        return False
    targets = {"EXAMES": r"exames?", "LEMBRETES": r"lembretes?|remedios?", "CONSULTAR_METRICAS": r"relatorios?|historico|medicoes",
               "ABRIR_LISTA_COMPRAS": r"lista|compras|mercado", "ABRIR_FAMILIA": r"familia|familiar"}
    if action in targets:
        return bool(re.search(r"\b(?:abr\w*|ver|mostr\w*|consult\w*|quero|lembra|avisa|adicion\w*|anot\w*)\b", text)
                    and re.search(r"\b(?:" + targets[action] + r")\b", text))
    if action == "REGISTRAR_AGUA":
        mentions = beverage_mentions(raw)
        return bool(len(mentions) == 1 and mentions[0]["beverage"] == "agua"
                    and mentions[0]["modality"] in ("report", "request") and mentions[0]["quantity_ml"]
                    and (params or {}).get("quantidade_ml") == mentions[0]["quantity_ml"])
    return False


def validated_label(raw, label, context=None):
    return label if action_allowed(raw, label, context=context) else "CONVERSA"


def validate_envelope(raw, envelope, context=None):
    if action_allowed(raw, envelope.get("acao"), envelope.get("parametros"), context):
        return envelope
    # Substitui também a fala: não pode anunciar uma ação removida.
    fala = beverage_reply(raw) or ("Acione o SAMU 192 ou procure atendimento imediato." if is_current_emergency(raw)
                                  else "Posso explicar sua dúvida sem abrir opções ou preparar registros. Qual detalhe deseja entender?")
    return {"fala": fala, "acao": "CONVERSA", "parametros": {}}


def original_text(text):
    match = re.fullmatch(r"<fala_usuario>(.*)</fala_usuario>", text, re.S)
    return match[1] if match else text
