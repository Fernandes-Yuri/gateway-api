"""Contrato de ações do agente (B0). Fonte única da verdade.

Toda promessa do assistente nasce daqui: sem executor registrado para
o canal, o rótulo nem existe para o classificador — fica impossível,
por construção, prometer o que não dá para cumprir.
"""


ACTION_CATALOG = {
    "REGISTRAR_PRESSAO": {
        "channels": ("voice",),
        "needs_confirmation": True,
        "slots": ("sistolica", "diastolica"),
        "closing": "Pressão {sistolica} por {diastolica} registrada.",
    },
    "REGISTRAR_GLICEMIA": {
        "channels": ("voice",),
        "needs_confirmation": True,
        "slots": ("glicemia",),
        "closing": "Glicemia {glicemia} registrada.",
    },
    "REGISTRAR_AGUA": {
        "channels": ("voice",),
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
        "channels": ("chat", "voice"),
        "needs_confirmation": False,
        "slots": (),
        "closing": "Acione o SAMU 192. Os links de socorro estão na resposta.",
    },
}

# source do dataset -> canal do contrato
SOURCE_CHANNEL = {
    "app_chat": "chat",
    "app_chat_router": "chat",
    "app_chat_followup": "chat",
}


def actions_for_channel(channel: str) -> tuple:
    """Rótulos com executor neste canal (para o classificador)."""
    return tuple(a for a, c in ACTION_CATALOG.items() if channel in c["channels"])


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
