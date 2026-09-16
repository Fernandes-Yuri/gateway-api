"""Curadoria do dataset Braga (Fase 5).

Separa amostras limpas das que cristalizariam delírio num fine-tuning:
- legacy-unconverted: schema antigo (prompt_usuario/resposta_braga).
- false-confirmation: fala afirma que salvou/abriu sem confirmação.
- invented-numbers: número nos parâmetros ausente na fala do usuário.
- diet-ghost: fala cita comida que o usuário não mencionou.

Uso:
    python3 curate_dataset.py [entrada] [dir_saida]
Nunca altera o arquivo original; escreve curated/quarantine + report.
"""

import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime

FALSE_CONFIRM_RE = re.compile(
    r"j[aá]\s+(registrei|registrado|salvei|salvo|abri|aberto|anotei|anotado)|"
    r"tudo\s+anotado|busquei\s+os\s+detalhes|tudo\s+lan[cç]ado",
    re.IGNORECASE,
)

FOOD_WORDS = {
    "arroz", "feijao", "frango", "carne", "peixe", "ovo", "ovos", "leite",
    "queijo", "pao", "banana", "maca", "laranja", "mamao", "salada",
    "tomate", "alface", "cenoura", "batata", "macarrao", "aveia", "iogurte",
    "cafe", "suco", "sopa", "bolacha", "biscoito", "manteiga", "fruta",
    "legumes", "verdura", "farofa", "couve", "brocolis", "melancia",
    "melao", "abacaxi", "beterraba", "espinafre", "aco", "acai", "tapioca",
    "cuscuz", "pamonha", "feijoada", "lasanha", "pizza", "hamburguer",
    "refrigerante", "cerveja", "vinho", "chocolate", "sorvete", "bolo",
    "azeite", "verduras", "alimento", "almoco", "jantar", "lanche",
}


def norm(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


def numbers(text: str):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def food_words(text: str):
    words = set(re.findall(r"[a-z]+", norm(text)))
    return words & FOOD_WORDS


def audit(entry: dict):
    """Retorna (veredito, motivos). Vereditos: keep, quarantine."""
    if "prompt_usuario" in entry or "messages" not in entry:
        return "quarantine", ["legacy-unconverted"]
    try:
        from orb_router import detect_all
        _nutrition_ctx = detect_all(
            " ".join(m.get("content", "") for m in entry.get("messages", [])
                      if m.get("role") == "user")
        ).get("kind") == "nutrition"
    except Exception:
        _nutrition_ctx = False
    reasons = []
    msgs = entry.get("messages", []) or []
    user_text = " ".join(m.get("content", "") for m in msgs if m.get("role") == "user")
    asst_text = " ".join(m.get("content", "") for m in msgs if m.get("role") == "assistant")
    meta = entry.get("messages", None) and entry.get("metadata", {}) or {}
    action = meta.get("action", "CONVERSA")
    params = meta.get("params", {}) or {}
    try:
        from orb_actions import ACTION_CATALOG, SOURCE_CHANNEL, actions_for_channel
        _channel = SOURCE_CHANNEL.get(entry.get("source", ""))
        if (_channel and action in ACTION_CATALOG
                and action not in actions_for_channel(_channel)):
            reasons.append(f"action-sem-executor:{action}/{_channel}")
    except Exception:
        pass

    if action != "CONVERSA" and FALSE_CONFIRM_RE.search(asst_text):
        reasons.append("false-confirmation")
    if isinstance(params, dict):
        param_nums = numbers(json.dumps(params, ensure_ascii=False))
        user_nums = numbers(user_text)
        # Normaliza "12 por 8" -> {12, 8} vs params {120, 80}: permite x10.
        expanded = set(user_nums)
        for n in list(user_nums):
            try:
                expanded.add(str(int(float(n.replace(",", ".")) * 10)))
            except ValueError:
                pass
        invented = {n for n in param_nums if n not in expanded and n not in user_text}
        if invented:
            reasons.append(f"invented-numbers:{sorted(invented)}")
    asst_food = food_words(asst_text)
    user_food = food_words(user_text)
    if asst_food - user_food and action in ("BUSCAR_ALIMENTO", "ABRIR_LISTA_COMPRAS", "CONVERSA"):
        if not user_food and not _nutrition_ctx:
            reasons.append(f"diet-ghost:{sorted(asst_food - user_food)[:5]}")
    if reasons:
        return "quarantine", reasons
    return "keep", []


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "dataset_braga.jsonl"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "curated"
    import os
    os.makedirs(outdir, exist_ok=True)
    kept, quarantined = [], []
    by_reason = Counter()
    by_source = Counter()
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            verdict, reasons = audit(entry)
            by_source[entry.get("source", "legacy")] += 1
            if verdict == "keep":
                entry.setdefault("metadata", {})["curated"] = True
                kept.append(entry)
            else:
                for r in reasons:
                    by_reason[r.split(":")[0]] += 1
                entry.setdefault("metadata", {})["quarantine_reasons"] = reasons
                quarantined.append(entry)
    stamp = datetime.now().strftime("%Y%m%d")
    with open(f"{outdir}/dataset_braga.curated-{stamp}.jsonl", "w", encoding="utf-8") as f:
        for e in kept:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(f"{outdir}/dataset_braga.quarantine-{stamp}.jsonl", "w", encoding="utf-8") as f:
        for e in quarantined:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    report = {
        "total": len(kept) + len(quarantined),
        "kept": len(kept),
        "quarantined": len(quarantined),
        "by_reason": dict(by_reason),
        "by_source": dict(by_source),
    }
    with open(f"{outdir}/curate-report-{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
