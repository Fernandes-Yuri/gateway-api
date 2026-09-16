#!/usr/bin/env python3
"""Leitor da trilha do LLM.

Uso:
    python3 trace_view.py [N]            # últimas N traces completas
    python3 trace_view.py --id ABC123    # uma trace específica
    python3 trace_view.py -- resumo      # contagem por canal/stage
"""
import json
import os
import sys
from collections import Counter

PATH = os.getenv("TRACE_PATH", "llm_trace.jsonl")


def load():
    rows = []
    if not os.path.exists(PATH):
        print(f"sem trilha em {PATH} — mande mensagens pelos canais primeiro.")
        return rows
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def show_trace(rows, tid):
    evs = [r for r in rows if r.get("trace_id") == tid]
    if not evs:
        print(f"trace {tid} não encontrada.")
        return
    print(f"=== TRACE {tid} ({len(evs)} eventos) ===")
    for e in evs:
        print(f"\n--- {e['ts']} [{e['channel']}] {e['stage']} ---")
        print(json.dumps(e.get("fields", {}), ensure_ascii=False, indent=2)[:4000])


def main():
    rows = load()
    if not rows:
        return
    args = sys.argv[1:]
    if args[:1] == ["--id"] and len(args) > 1:
        show_trace(rows, args[1])
        return
    if args[:1] == ["--resumo"] or args[:1] == ["resumo"]:
        c = Counter((r.get("channel"), r.get("stage")) for r in rows)
        for (ch, st), n in sorted(c.items()):
            print(f"{n:5d}  {ch:10s} {st}")
        tids = sorted({r.get("trace_id") for r in rows})
        print(f"\n{len(tids)} traces. Últimas: {', '.join(tids[-5:])}")
        return
    n = int(args[0]) if args[:1] and args[0].isdigit() else 3
    tids = sorted({r.get("trace_id") for r in rows})[-n:]
    for tid in tids:
        show_trace(rows, tid)


if __name__ == "__main__":
    main()
