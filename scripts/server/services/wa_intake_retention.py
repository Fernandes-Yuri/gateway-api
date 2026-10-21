"""Prazo absoluto de dados ativos; não equivale ao ciclo de backups."""
from datetime import datetime, timezone

RETENTION_SECONDS = 23 * 3600 + 59 * 60
# Política fixa: configurações legadas em dias não podem prolongar a guarda.
ACTIVE_SQL = "created_at > clock_timestamp() - INTERVAL '23 hours 59 minutes'"


def expired(row):
    return (datetime.now(timezone.utc) - row['created_at']).total_seconds() >= RETENTION_SECONDS
