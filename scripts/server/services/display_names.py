"""Identificação reduzida nas respostas do assistente."""
from collections import Counter


def first_name(name):
    return str(name or '').strip().split()[0] if str(name or '').strip() else 'seu familiar'


def choice_labels(rows):
    names = [first_name(row['full_name']) for row in rows]
    counts = Counter(names)
    return [f'{name} (opção {i + 1})' if counts[name] > 1 else name for i, name in enumerate(names)]
