"""Vereditos de conteúdo independentes do transporte do harness WhatsApp.

Aceita checks estruturados ou DEVE/NÃO DEVE com /expressão regular/.
Expectativas em prosa permanecem inconclusivas para revisão humana.
"""
import re
import unicodedata


def _normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if not unicodedata.combining(c))


def evaluate(record):
    result = dict(record)
    if record.get('status') != 'ok':
        return {**result, 'veredito': 'reprovado', 'motivos': ['Falha de transporte ou ausência de resposta.']}
    reply = str(record.get('resposta') or '')
    if not reply.strip():
        return {**result, 'veredito': 'reprovado', 'motivos': ['Resposta vazia.']}
    # O título sozinho não contém o valor nem prova o conteúdo do cartão.
    if _normalize(reply.strip()) == 'confirmacao de registro':
        return {**result, 'veredito': 'inconclusivo', 'motivos': ['Corpo do cartão não capturado.']}
    checks = record.get('checks')
    if checks is None:
        checks = []
        for line in str(record.get('esperado') or '').splitlines():
            if len(re.findall(r'(?:^|[·;]\s*)(?:DEVE|N[ÃA]O DEVE):\s*/', line, re.I)) > 1:
                return {**result, 'veredito': 'inconclusivo', 'motivos': ['Separe DEVE/NÃO DEVE em linhas distintas ou use checks estruturados.']}
            match = re.fullmatch(r'\s*(DEVE|N[ÃA]O DEVE):\s*/(.+)/\s*', line, re.I)
            if not match:
                # Nunca trata uma expectativa parcialmente entendida como aprovação.
                return {**result, 'veredito': 'inconclusivo', 'motivos': ['Expectativa exige revisão humana; use checks ou DEVE: /regex/.']}
            checks.append({'pattern': match[2], 'present': _normalize(match[1]) == 'deve'})
    if not isinstance(checks, list) or not checks:
        return {**result, 'veredito': 'inconclusivo', 'motivos': ['Sem verificações de conteúdo.']}
    failures = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('pattern'), str) or type(check.get('present')) is not bool:
            return {**result, 'veredito': 'inconclusivo', 'motivos': ['Verificação malformada.']}
        try:
            found = bool(re.search(check['pattern'], reply, re.I))
        except re.error:
            return {**result, 'veredito': 'inconclusivo', 'motivos': ['Expressão regular inválida.']}
        if found != check['present']:
            failures.append(('Faltou: ' if check['present'] else 'Conteúdo proibido: ') + check['pattern'])
    return {**result, 'veredito': 'reprovado' if failures else 'aprovado', 'motivos': failures}
