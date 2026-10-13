"""Controle de publicação local por Unix socket, sem credenciais em SSM.

Somente o usuário do gateway/root pode invocar o canal (SO_PEERCRED e 0600).
Não existe rota administrativa TCP nem leitura de .env neste protocolo.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import struct

from release_publisher import ReleaseManifest, publish_release, SIGNING_CERT_SHA256


async def start_release_control(root, pool_getter, public_base_url, notify=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'publish.sock'
    if path.is_symlink():
        raise RuntimeError('Socket de publicação não pode ser um link')
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('Caminho de controle ocupado por arquivo não autorizado')
        # Nunca remover o socket de outro worker vivo.
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise RuntimeError('Controle de publicação já está ativo')
        finally:
            probe.close()

    async def handle(reader, writer):
        response = {'status': 'error', 'error': 'publicação recusada'}
        try:
            sock = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in {0, os.getuid()}:
                raise PermissionError('Usuário não autorizado')
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            payload = json.loads(raw)
            if payload == {'action': 'probe'}:
                pool = pool_getter()
                if pool is None:
                    raise RuntimeError('Banco indisponível')
                async with pool.acquire() as conn:
                    if not await conn.fetchval("SELECT to_regclass('app_releases')"):
                        raise RuntimeError('Catálogo indisponível')
                response = {'status': 'ready', 'contract': 2, 'certificate_sha256': SIGNING_CERT_SHA256}
                return
            manifest = ReleaseManifest.model_validate(payload)
            response = await publish_release(pool_getter(), root, manifest, public_base_url)
            if response['status'] == 'published' and notify is not None:
                try:
                    result = await notify(manifest)
                    response['notification'] = 'completed'
                    if isinstance(result, dict):
                        response['sent_count'] = result.get('sent_count', 0)
                        response['failed_count'] = result.get('failed_count', 0)
                except Exception:
                    response['notification'] = 'failed'
        except Exception as error:
            # Nenhum payload, conteúdo do banco ou credencial aparece na saída.
            response['error'] = type(error).__name__
        finally:
            writer.write(json.dumps(response).encode() + b'\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=8192)
    path.chmod(0o600)
    return server, path
