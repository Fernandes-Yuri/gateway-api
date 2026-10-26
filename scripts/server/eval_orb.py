"""Eval fixa do ORB (Fase 5) — a nota do refino.

Camada offline (sempre): ~50 casos sobre o router determinístico.
Camada viva (--live, só no servidor): classificador + fala via Groq,
com regras anti-delírio (sem número inventado, sem fantasma de dieta).

Uso:
    python3 eval_orb.py [--live] [--min-nota 90]
Saída: JSON + nota 0-100. Exit 1 se abaixo do mínimo.
"""

import json
import re
import sys
import unicodedata

from orb_router import detect_all, resolve_followup, route_chat
from orb_llm import parse_label

FOOD_WORDS = {
    "arroz", "feijao", "frango", "carne", "peixe", "ovo", "ovos", "leite",
    "queijo", "pao", "banana", "maca", "laranja", "salada", "tomate",
    "batata", "macarrao", "aveia", "iogurte", "cafe", "suco", "sopa",
    "manteiga", "fruta", "legumes", "couve", "melancia", "abacaxi",
    "beterraba", "espinafre", "chocolate", "bolo", "pizza", "azeite",
    "almoco", "jantar", "lanche", "dieta", "proteina", "salada",
}


def _norm(t):
    n = unicodedata.normalize("NFD", t or "")
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", n.lower()).strip()


def _nums(t):
    return {n for n in re.findall(r"\d+(?:[.,]\d+)?", t or "") if n not in ("192",)}


def _foods(t):
    return set(re.findall(r"[a-z]+", _norm(t))) & FOOD_WORDS


# (texto, kind esperado, checks extras)
KIND_CASES = [
    ("pressão 12 por 8", "bp", {"sys": 120, "dia": 80}),
    ("tô com 13 por 8 e meio", "bp", {"sys": 130, "dia": 85}),
    ("minha pressão deu 16 por 10", "bp", {"sys": 160, "dia": 100}),
    ("glicemia 105", "glucose", {"valor": 105}),
    ("105 de glicose", "glucose", {"valor": 105}),
    ("tomei dois copos de água", "hydration", {"ml": 500}),
    ("bebi 500 ml", "hydration", {"ml": 500}),
    ("batimentos 72", "hr", {"bpm": 72}),
    ("saturação 97", "spo2", {"valor": 97}),
    ("dor no peito", "emergency", {}),
    ("falta de ar", "emergency", {}),
    ("me lembra do remédio", "reminder", {}),
    ("quero ver meus exames", "exams", {}),
    ("tomei meu remédio", "meds", {}),
    ("almocei arroz e feijão", "nutrition", {}),
    ("o que posso almoçar?", "nutrition", {}),
    ("bom dia", "greeting_morning", {}),
    ("boa noite, vou dormir", "greeting_evening", {}),
    ("oi, tudo bem?", "greeting", {}),
    ("obrigado", "gratitude", {}),
    ("onde baixo o app?", "app", {}),
    ("quem é você?", "identity", {}),
    ("minha pressão anda estranha", "topic", {}),
    ("estou com diarreia desde cedo", "symptom_gi", {}),
    ("tô com dor de cabeça também", "symptom_headache", {}),
    ("você tem dor de barriga?", "bot", {}),
    ("tô na correria aqui", "smalltalk", {}),
    ("vou descer ali rapidinho", "smalltalk", {}),
    ("beleza então", "smalltalk", {}),
    ("adiciona aveia e banana na lista", "grocery", {}),
    ("abre minha lista de compras", "grocery", {}),
    ("por que o céu é azul?", "open", {}),
    ("me conta sobre aposentadoria hoje?", "open", {}),
]

# (texto, ação esperada do envelope, palavras proibidas na fala)
GHOST_CASES = [
    ("tô na correria aqui", "CONVERSA", None),
    ("vou descer ali rapidinho", "CONVERSA", None),
    ("bom dia, vou descer ali", "CONVERSA", ["refeição", "comeu"]),
    ("legal, entendi", "CONVERSA", None),
    ("haha boa", "CONVERSA", None),
    ("almocei arroz e feijão", "CONVERSA", None),
]

# (texto, registrado?, tipo esperado)
FOLLOWUP_CASES = [
    ("almocei arroz", "nutrition", "já comi", "ate"),
    ("almocei arroz", "nutrition", "põe na lista", "grocery"),
    ("almocei arroz", "nutrition", "sim", "none"),
    ("oi", "greeting", "já comi", "none"),
    ("almocei arroz", None, "já comi", "none"),
]

# Casos vivos: (texto, label esperado, permite_comida_explicita?)
LIVE_CASES = [
    ("por que o céu é azul?", "CONVERSA", False),
    ("me conta uma história do mar", "CONVERSA", False),
    ("quero ver meu relatório de medições", "CONSULTAR_METRICAS", False),
    ("abre meus lembretes de remédio", "LEMBRETES", False),
    ("quero ver meus exames", "EXAMES", False),
    ("tô na correria aqui", "CONVERSA", False),
    ("o que posso almoçar?", "CONVERSA", True),
    ("como está minha pressão?", "CONVERSA", False),
]


def run_offline():
    results = {"categorias": {}, "falhas": []}
    total = passed = 0

    def check(cat, name, ok, detail=""):
        nonlocal total, passed
        total += 1
        c = results["categorias"].setdefault(cat, {"pass": 0, "total": 0})
        c["total"] += 1
        if ok:
            passed += 1
            c["pass"] += 1
        else:
            results["falhas"].append(f"[{cat}] {name}: {detail}")

    for text, kind, vals in KIND_CASES:
        det = detect_all(text)
        ok = det.get("kind") == kind and all(det.get(k) == v for k, v in vals.items())
        check("kinds", text, ok, f"esperava {kind}{vals}, veio {det}")

    for text, action, forbidden in GHOST_CASES:
        env = route_chat(text)
        user_foods = _foods(text)
        fala_foods = _foods(env["fala"]) if env else set()
        ghost = bool(fala_foods - user_foods)
        bad_word = forbidden and env and any(w in _norm(env["fala"]) for w in forbidden)
        nums_ok = True
        if env:
            for n in _nums(env["fala"]):
                if n not in _nums(text):
                    try:
                        if str(int(float(n.replace(",", ".")) / 10)) not in _nums(text):
                            nums_ok = False
                    except ValueError:
                        nums_ok = False
        ok = env is not None and env["acao"] == action and not ghost and not bad_word and nums_ok
        check("anti_fantasma", text, ok,
              f"acao={env['acao'] if env else None} ghost={ghost} bad={bad_word} nums={nums_ok}")

    for prev, kind, reply, want in FOLLOWUP_CASES:
        env = resolve_followup(reply, kind, channel="chat")
        if env is None:
            got = "none"
        elif "Que bom!" in env["fala"]:
            got = "ate"
        elif "Combinado!" in env["fala"]:
            got = "grocery"
        else:
            got = "unknown"
        check("followup", f"{prev} -> {reply}", got == want, f"veio {got}")

    results["nota_offline"] = round(100 * passed / total) if total else 0
    results["total"] = total
    results["passed"] = passed
    return results


def run_live():
    import os
    import httpx
    # AUD-G(B): SPEAK_SYSTEM removido do import — nao era usado neste bloco.
    from orb_llm import (build_classify_payload, build_speak_payload)
    key = os.getenv("GROQ_API_KEY") or os.getenv("HF_TOKEN")
    url = "https://api.groq.com/openai/v1/chat/completions"
    model = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    if not key:
        return {"skipped": "sem GROQ_API_KEY"}
    results = {"casos": [], "falhas": []}
    passed = 0
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as client:
        for text, want_label, permite_comida in LIVE_CASES:
            row = {"texto": text, "esperado": want_label, "ok": False}
            try:
                cr = client.post(url, json=build_classify_payload(model, text), headers=headers)
                label = parse_label(cr.json()["choices"][0]["message"]["content"])
                row["label"] = label
                ok = label == want_label
                if ok and label == "CONVERSA":
                    sp = build_speak_payload(model, [{"role": "user", "content": text}], 0.1, 220)
                    sr = client.post(url, json=sp, headers=headers)
                    fala = (sr.json()["choices"][0]["message"]["content"] or "").strip()
                    row["fala"] = fala[:160]
                    bad_struct = any(t in fala for t in ("{", "}", "```"))
                    invented = any(n not in _nums(text) for n in _nums(fala))
                    ghost = bool(_foods(fala) - _foods(text)) and not permite_comida
                    ok = bool(fala) and not bad_struct and not invented and not ghost
                    row.update({"estrutura_ok": not bad_struct, "sem_numero_inventado": not invented, "sem_fantasma": not ghost})
                row["ok"] = ok
            except Exception as e:
                row["erro"] = str(e)[:120]
            if row["ok"]:
                passed += 1
            else:
                results["falhas"].append(row)
            results["casos"].append(row)
    results["nota_live"] = round(100 * passed / len(LIVE_CASES))
    return results


def main():
    live = "--live" in sys.argv
    min_nota = 90
    for i, a in enumerate(sys.argv):
        if a == "--min-nota" and i + 1 < len(sys.argv):
            min_nota = int(sys.argv[i + 1])
    report = run_offline()
    if live:
        report["live"] = run_live()
        nota = report["live"].get("nota_live", report["nota_offline"])
    else:
        nota = report["nota_offline"]
    report["nota"] = nota
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if nota >= min_nota else 1)


if __name__ == "__main__":
    main()
