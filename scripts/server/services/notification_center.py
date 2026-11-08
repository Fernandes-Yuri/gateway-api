"""Caixa persistente compartilhada por Python e Go. Não grava texto clínico ou de chat."""
import json
from datetime import datetime, timezone


async def emit_notification(conn, *, recipient_id, event_key, group_key, kind, category,
                            resource_type, resource_id, patient_id=None, actor_id=None,
                            occurred_at=None, expires_at=None, payload=None, realert=True):
    return await conn.fetchval('SELECT notification_emit($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13)',
        recipient_id, event_key, group_key, kind, category, resource_type, str(resource_id), patient_id, actor_id,
        occurred_at or datetime.now(timezone.utc), expires_at, json.dumps(payload or {}), realert)


async def remove_notification(conn, recipient_id, event_key):
    await conn.execute('SELECT notification_remove($1,$2)', recipient_id, event_key)


async def remove_resource_notifications(conn, resource_type, resource_id):
    await conn.execute('SELECT notification_remove_resource($1,$2)', resource_type, str(resource_id))


async def block_notification_pair(conn, actor_id, target_id):
    await conn.execute('SELECT notification_block_pair($1,$2)', actor_id, target_id)
