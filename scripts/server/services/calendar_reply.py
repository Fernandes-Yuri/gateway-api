"""Respostas de calendário calculadas, sem interpretação pelo modelo."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo')
MONTHS = ('', 'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto',
          'setembro', 'outubro', 'novembro', 'dezembro')


def calendar_reply(text, kind, timezone_name='America/Sao_Paulo', now=None):
    try:
        zone = ZoneInfo(timezone_name or 'America/Sao_Paulo')
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo('America/Sao_Paulo')
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    if kind == 'time':
        return f"Agora são {current.hour} horas e {current.minute} minutos."
    literal = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', text)
    if literal and 'hoje' not in text.casefold():
        try:
            date = datetime.strptime(literal[1], '%d/%m/%Y')
        except ValueError:
            return 'Essa data não é válida. Qual data você quer consultar?'
        return f"{literal[1]} é {WEEKDAYS[date.weekday()]}: {date.day} de {MONTHS[date.month]} de {date.year}."
    reply = f"Hoje é {WEEKDAYS[current.weekday()]}, {current.day} de {MONTHS[current.month]} de {current.year}."
    if literal:
        reply += f' A data citada na pergunta foi {literal[1]}.'
    return reply
