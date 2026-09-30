"""Metodos de gerenciamento de dispositivos, tokens push (PostgreSQL) e FCM.

Higiene Fase B: persistencia relacional via tabela device_push_tokens, com suporte
a transicao suave a partir do legado registered_devices.json e compatibilidade in-memory.
"""
import json
import os
from typing import Any, Dict, List, Optional

from config import DEVICES_PATH


def load_devices() -> Dict[str, Any]:
    """Fallback in-memory/arquivo para compatibilidade com testes legados."""
    if not os.path.exists(DEVICES_PATH):
        return {}
    try:
        with open(DEVICES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_devices(data: Dict[str, Any]):
    """Fallback de persistencia em arquivo para compatibilidade com testes legados."""
    try:
        with open(DEVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def register_device_token(
    pool,
    user_id: str,
    token: str,
    role: str = "caregiver",
    platform: str = "fcm",
) -> bool:
    """Registra ou atualiza um token push relacionalmente via UPSERT."""
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO device_push_tokens (token, user_id, role, platform, is_active, last_seen_at)
            VALUES ($1, $2, $3, $4, true, NOW())
            ON CONFLICT (token) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                role = EXCLUDED.role,
                platform = EXCLUDED.platform,
                is_active = true,
                last_seen_at = NOW();
            """,
            token,
            user_id,
            role,
            platform,
        )
    return True


async def get_active_tokens_for_caregivers(pool, exclude_user_id: Optional[str] = None) -> List[str]:
    """Retorna lista de tokens FCM ativos de cuidadores."""
    if not pool:
        return []
    async with pool.acquire() as conn:
        if exclude_user_id:
            rows = await conn.fetch(
                """
                SELECT token FROM device_push_tokens
                WHERE LOWER(role) = 'caregiver' AND is_active = true AND user_id != $1;
                """,
                exclude_user_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT token FROM device_push_tokens
                WHERE LOWER(role) = 'caregiver' AND is_active = true;
                """
            )
        if rows and isinstance(rows, (list, tuple)):
            return [r["token"] for r in rows]
        return []


async def get_active_devices_for_users(pool) -> List[Dict[str, Any]]:
    """Retorna lista de dispositivos ativos registrados no sistema."""
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT token, user_id, role, platform FROM device_push_tokens
            WHERE is_active = true;
            """
        )
        if rows and isinstance(rows, (list, tuple)):
            return [dict(r) for r in rows]
        return []


async def load_devices_by_user(pool, legacy_loader=None) -> Dict[str, List[Dict[str, Any]]]:
    """Dispositivos ativos agrupados por usuario, com suporte a multi-device.

    Plano B (doc 10 §1B): a fonte de verdade do push e a tabela
    device_push_tokens — o registered_devices.json era consumido pela migracao
    de startup, deixando o broadcast lendo 0 dispositivos. Sem pool, ou quando
    o banco nao responde/sem linhas, cai no loader legado (JSON): no
    pos-deploy o arquivo vira apenas fallback de disponibilidade.
    """
    def _legacy() -> Dict[str, List[Dict[str, Any]]]:
        raw = (legacy_loader or load_devices)() or {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for user_id, dev in raw.items():
            if isinstance(dev, dict) and dev.get("token"):
                entry = dict(dev)
                entry.setdefault("userId", user_id)
                grouped.setdefault(user_id, []).append(entry)
        return grouped

    if not pool:
        return _legacy()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token, user_id, role, platform FROM device_push_tokens WHERE is_active = true;"
            )
        grouped = {}
        for row in rows or []:
            token = row.get("token")
            if not token:
                continue
            user_id = row.get("user_id")
            grouped.setdefault(user_id, []).append({
                "userId": user_id,
                "token": token,
                "role": row.get("role"),
                "platform": row.get("platform"),
            })
        if grouped:
            return grouped
    except Exception as error:
        print(f"[Devices] Falha ao ler device_push_tokens; usando JSON legado: {error}")
    return _legacy()


async def migrate_legacy_json_devices(pool) -> int:
    """Migra registros legados de registered_devices.json para PostgreSQL.

    Ao concluir com sucesso, renomeia o arquivo para registered_devices.json.migrated
    (nunca deleta silenciosamente sem backup).
    """
    if not pool or not os.path.exists(DEVICES_PATH):
        return 0
    try:
        with open(DEVICES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Device Migration Error] Falha ao ler {DEVICES_PATH}: {e}")
        return 0

    if not isinstance(data, dict) or not data:
        return 0

    count = 0
    for key, info in data.items():
        if isinstance(info, dict):
            token = info.get("token") or key
            user_id = info.get("userId") or info.get("user_id") or key
            role = info.get("role") or "caregiver"
            platform = info.get("platform") or "fcm"
        else:
            token = str(info)
            user_id = str(key)
            role = "caregiver"
            platform = "fcm"

        if not token:
            continue

        try:
            await register_device_token(pool, user_id=user_id, token=token, role=role, platform=platform)
            count += 1
        except Exception as e:
            print(f"[Device Migration Error] Falha ao migrar token {token[:10]}...: {e}")

    try:
        backup_path = f"{DEVICES_PATH}.migrated"
        os.replace(DEVICES_PATH, backup_path)
        print(f"[Device Migration] {count} dispositivos migrados para o banco. Arquivo renomeado para {backup_path}")
    except Exception as e:
        print(f"[Device Migration Error] Falha ao renomear {DEVICES_PATH}: {e}")

    return count


def dispatch_push(token: str, title: str, body: str, data: Dict[str, str] = None,
                  include_notification: bool = True, messaging=None, has_fcm: bool = False) -> bool:
    """Envia push FCM. messaging/has_fcm vem do modulo importador (estado global
    do server); mantidos explicitos para que este modulo fique testavel."""
    if has_fcm and messaging is not None:
        try:
            # Prioridade ALTA e canal explicito: sem isso o Android em repouso
            # (Doze) nao acorda e a mensagem fica enfileirada silenciosamente.
            # O canal family_alerts e o padrao declarado no AndroidManifest
            # (com.google.firebase.messaging.default_notification_channel_id).
            android_cfg = messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="bragasaude_family_alerts",
                    priority="high",
                    sound="default",
                    # Mantem o tipo no payload: com app fechado o SO desenha o
                    # balao (onMessageReceived nao roda); com app aberto o
                    # service le data["type"] e faz o deep link.
                    click_action="OPEN_FAMILY_MESSAGES" if (data or {}).get("type") == "FAMILY_MESSAGE" else None,
                ),
            )
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body) if include_notification else None,
                data=data or {},
                android=android_cfg,
                token=token
            )
            messaging.send(message)
            return True
        except Exception as e:
            print(f"[FCM Error] {e}")
            return False
    else:
        # doc 10 §1B.2: o mock NAO devolve mais True -- sucesso fingido mascarava
        # push nunca enviado e fazia os endpoints reportarem enviados que nao
        # aconteceram. Registra a intencao e devolve False.
        print(f"[FCM Mock — NAO ENVIADO] Push para {token[:10]}...: {title} - {body}")
        return False
