"""Adaptador para consumo do Rate Limiter centralizado em Rust via Socket Unix IPC (Fase 1C)."""
import json
import logging
import socket
from typing import Optional

logger = logging.getLogger("braga.limiter_adapter")

DEFAULT_LIMITER_SOCK = "/run/braga-saude/limiter.sock"


class RustLimiterClient:
    """Cliente leve síncrono/assíncrono para consultar o limiter centralizado em Rust."""

    def __init__(self, socket_path: str = DEFAULT_LIMITER_SOCK):
        self.socket_path = socket_path

    def _send_ipc(self, payload: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.5)
                client.connect(self.socket_path)
                data = json.dumps(payload) + "\n"
                client.sendall(data.encode("utf-8"))
                response_data = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if b"\n" in response_data:
                        break
                if not response_data:
                    return {"status": "error", "message": "Sem resposta do limiter"}
                return json.loads(response_data.decode("utf-8").strip())
        except Exception as e:
            logger.warning("[RustLimiterClient] Falha ao comunicar com limiter via Unix socket: %s", e)
            return {"status": "error", "message": str(e)}

    def check(
        self,
        scope: str,
        key: str,
        limit: int,
        window_seconds: int,
        ip: Optional[str] = None,
    ) -> dict:
        req = {
            "action": "check",
            "scope": scope,
            "key": key,
            "limit": limit,
            "window_seconds": window_seconds,
            "ip": ip,
        }
        return self._send_ipc(req)

    def record_violation(self, ip: str, reason: str, severity: int = 1) -> None:
        req = {
            "action": "record_violation",
            "ip": ip,
            "reason": reason,
            "severity": severity,
        }
        self._send_ipc(req)


# Instância global do adaptador
limiter_client = RustLimiterClient()
