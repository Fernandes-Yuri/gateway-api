#!/usr/bin/env python3
"""Lê JSONL do harness; escreve JSONL com veredito sem alterar os originais."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'server'))
from regression_verdict import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    failed = False
    with args.input.open(encoding='utf-8') as source:
        for line in source:
            if line.strip():
                result = evaluate(json.loads(line))
                print(json.dumps(result, ensure_ascii=False))
                failed |= result['veredito'] != 'aprovado'
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
