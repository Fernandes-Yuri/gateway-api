"""Reconhecimento de pedidos de preferências no assistente do aplicativo."""
import re
import unicodedata
from typing import Optional

def _norm(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


_LIST_RE = re.compile(
    r"\b(o que voce lembra|o que vc lembra|o que voce sabe de mim|"
    r"o que vc sabe de mim|quais (sao|sao as) (as )?preferencias|"
    r"minhas preferencias|o que voce guardou|o que vc guardou)\b")

_FORGET_ALL_RE = re.compile(
    r"\b(esquece (tudo|todas|as preferencias|o que sabe|o que voce sabe|"
    r"o que vc sabe))|\bapaga (tudo|todas (as )?preferencias)\b|"
    r"\bnao quero que (voce|vc) (lembre|guarde|saiba) (nada|de mim)\b")

_SET_NICKNAME_RE = re.compile(
    r"\b(me chama de|me chame de|pode me chamar de|gostaria de ser chamad[oa] de|"
    r"quero ser chamad[oa] de)\s+([A-Za-zÀ-ÿ'' -]{1,40})")

_SET_LENGTH_RE = re.compile(
    r"\b(respostas? (mais )?(curtas|longas|detalhadas)|"
    r"prefiro respostas? (mais )?(curtas|longas|detalhadas)|"
    r"responde (mais )?(curto|longo|detalhado)|"
    r"quero respostas? (curtas|longas|detalhadas|equilibradas))\b")

_FORGET_ONE_RE = re.compile(
    r"\b(esquece|apaga|remove)\b.{0,40}\b"
    r"(apelido|copo|respostas|extensao|tamanho)\b")

# Passo 5.3: consulta de UMA preferência ("qual meu apelido?"), além do
# "o que você lembra de mim?" que lista tudo. Padrões específicos para
# não confundir "qual copo devo usar?" com consulta de preferência.
_QUERY_ONE_RE = re.compile(
    r"\b(qual|o que e|qual e|me diz|me fala)\b.{0,20}\b"
    r"(apelido|tamanho do (meu )?copo|volume do (meu )?copo|"
    r"extensao das (minhas )?respostas)\b"
    r"|como voce (me )?(chama|chamaria)")

# Passo 5.3: volume habitual do copo ("meu copo é de 200 ml").
_SET_CUP_RE = re.compile(
    r"\b(meu copo (e|tem)?\s*(de|e)?|copo (de|com|e)|bebo (no|em|de)?\s*(um|copo)?\s*"
    r"(copo\s*(de|com)?|de)?)\s*(\d{2,4})\s*(ml|mililitros?)\b",
    re.IGNORECASE)


def _target_key(norm: str) -> Optional[str]:
    """Resolve a chave (nickname/cup_ml/response_length) da preferência citada.

    Tolerante a possessivos e variações ("tamanho do MEU copo",
    "como você ME chama", "extensão das MINHAS respostas").
    """
    if re.search(r"\bapelido\b|\bcomo voce (me )?chama\w*\b|"
                 r"\bnome que voce (me )?chama\w*\b", norm):
        return "nickname"
    if re.search(r"\bcopo\b|\btamanho do copo\b|\bvolume do copo\b", norm):
        return "cup_ml"
    if re.search(r"\bextensao das (minhas )?respostas\b|"
                 r"\brespostas (curtas|longas|detalhadas|equilibradas)\b", norm):
        return "response_length"
    return None


def _length_value(norm: str) -> Optional[str]:
    m = re.search(r"\b(curtas|curto)\b", norm)
    if m:
        return "short"
    m = re.search(r"\b(detalhadas|detalhado|longas|longo)\b", norm)
    if m:
        return "detailed"
    m = re.search(r"\b(equilibradas|equilibrada)\b", norm)
    if m:
        return "balanced"
    return None


def detect_preference_intent(text: str) -> Optional[dict]:
    """
    Intenção determinística de preferência (passo 5.3), sem LLM.

    Retorna None quando a fala não é de preferência (segue o fluxo normal).
    """
    norm = _norm(text)
    if not norm:
        return None

    if _LIST_RE.search(norm):
        return {"op": "list"}

    if _FORGET_ALL_RE.search(norm):
        return {"op": "forget", "key": "all"}

    if _FORGET_ONE_RE.search(norm):
        key = _target_key(norm)
        if key:
            return {"op": "forget", "key": key}

    # Passo 5.3: consulta de uma única preferência.
    if _QUERY_ONE_RE.search(norm):
        key = _target_key(norm)
        if key:
            return {"op": "list", "key": key}

    m = _SET_NICKNAME_RE.search(norm)
    if m:
        apelido = m.group(2).strip()
        if 1 <= len(apelido) <= 40 and all(c.isalpha() or c in " '-" for c in apelido):
            return {"op": "set", "key": "nickname", "value": apelido}

    if _SET_LENGTH_RE.search(norm):
        valor = _length_value(norm)
        if valor:
            return {"op": "set", "key": "response_length", "value": valor}

    m = _SET_CUP_RE.search(norm)
    if m:
        volume = int(m.group(9))
        if 50 <= volume <= 2000:
            return {"op": "set", "key": "cup_ml", "value": volume}

    return None


