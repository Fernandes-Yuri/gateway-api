"""Metodos auxiliares de dispositivos e FCM.

Extraidos do monolito server.py (linhas 771-823) na Fase A passo 2.
Sem dependencia do FastAPI; usam config.DEVICES_PATH e o estado HAS_FCM
do modulo importador (passado por parametro para dispatch_push).
"""
import json
import os
from typing import Any, Dict

from config import DEVICES_PATH


def load_devices() -> Dict[str, Any]:
    if not os.path.exists(DEVICES_PATH):
        return {}
    try:
        with open(DEVICES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_devices(data: Dict[str, Any]):
    try:
        with open(DEVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
