"""Contrato de ações do agente (B0). Fonte única da verdade.

Toda promessa do assistente nasce daqui: sem executor registrado para
o canal, o rótulo nem existe para o classificador — fica impossível,
por construção, prometer o que não dá para cumprir.
"""

import re

ACTION_CATALOG = {
    "ABRIR_LISTA_COMPRAS": {
        "channels": ("chat",),
        "needs_confirmation": False,  # a tela pré-preenchida É a confirmação
        "slots": ("items",),
        "closing": "Lista aberta com {items}. Confirma na tela?",
    },
    "REGISTRAR_PRESSAO": {
        "channels": ("voice", "whatsapp"),
        "needs_confirmation": True,
        "slots": ("sistolica", "diastolica"),
        "closing": "Pressão {sistolica} por {diastolica} registrada.",
    },
    "REGISTRAR_GLICEMIA": {
        "channels": ("voice", "whatsapp"),
        "needs_confirmation": True,
        "slots": ("glicemia",),
        "closing": "Glicemia {glicemia} registrada.",
    },
    "REGISTRAR_AGUA": {
        "channels": ("voice", "whatsapp"),
        "needs_confirmation": True,
        "slots": ("quantidade_ml",),
        "closing": "{quantidade_ml} ml registrados.",
    },
    "CONSULTAR_METRICAS": {
        "channels": ("chat",),
        "needs_confirmation": False,
        "slots": (),
        "closing": "Relatório aberto para você conferir.",
    },
    "LEMBRETES": {
        "channels": ("chat",),
        "needs_confirmation": False,
        "slots": (),
        "closing": "Lembretes abertos.",
    },
    "EXAMES": {
        "channels": ("chat",),
        "needs_confirmation": False,
        "slots": (),
        "closing": "Exames abertos.",
    },
    "EMERGENCIA": {
        "channels": ("chat", "voice", "whatsapp"),
        "needs_confirmation": False,
        "slots": (),
        "closing": "Opções de socorro na tela.",
    },
}

# source do dataset -> canal do contrato
SOURCE_CHANNEL = {
    "app_chat": "chat",
    "app_chat_router": "chat",
    "app_chat_followup": "chat",
    "whatsapp": "whatsapp",
    "whatsapp_voice": "whatsapp",
    "whatsapp_router": "whatsapp",
    "whatsapp_voice_router": "whatsapp",
}


def actions_for_channel(channel: str) -> tuple:
    """Rótulos com executor neste canal (para o classificador)."""
    return tuple(a for a, c in ACTION_CATALOG.items() if channel in c["channels"])


def extract_grocery_items(text: str, max_items: int = 10) -> list:
    """Slots de lista em texto livre (sem catálogo — lista aceita qualquer item)."""
    t = (text or "").lower()
    if not re.search(r"(adiciona|adicione|adicionar|coloca|coloque|colocar|anota|anote|anotar|"
                      r"abre|abrir|abro|abra|compra|comprar|compre|preciso de|precisa de|falta|lista de compras?)", t):
        return []
    t = re.sub(
        r".*?(adiciona|adicione|adicionar|coloca|coloque|colocar|anota|anote|anotar|"
        r"abre|abrir|abro|abra|compra|comprar|compre|preciso de|precisa de|falta|lista de compras?)\s+",
        "", t, count=1)
    for _ in range(3):
        smaller = re.sub(r"(?:\s+|^)(minha lista|sua lista|a lista|minhas compras|na lista|no mercado|da lista|de compras?|na|no|pra|para|na minha|no meu|da semana)\s*$", "", t)
        if smaller == t:
            break
        t = smaller
    items = [i.strip(" .") for i in re.split(r",|\be\b", t) if len(i.strip()) > 2]
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:max_items]


def closing_fala(action: str, params: dict) -> str:
    """Fala de fechamento montada do recibo — nunca inventada."""
    try:
        spec = ACTION_CATALOG[action]
        filled = dict(params or {})
        if "items" in filled and isinstance(filled["items"], list):
            filled["items"] = ", ".join(str(x) for x in filled["items"])
        return spec["closing"].format(**filled)
    except Exception:
        return "Pronto! Confirma na tela?"
