"""Testes unitários para o RustLimiterClient (síncrono e assíncrono via Unix socket) - DT-062."""
import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from rust_limiter_adapter import RustLimiterClient


class TestRustLimiterAdapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sock_path = os.path.join(self.temp_dir.name, "test_limiter.sock")
        self.server = None

    async def asyncTearDown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.temp_dir.cleanup()

    async def _start_mock_server(self, response_dict):
        async def handle_client(reader, writer):
            data = await reader.readline()
            if data:
                writer.write((json.dumps(response_dict) + "\n").encode("utf-8"))
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_unix_server(handle_client, path=self.sock_path)

    async def test_check_async_allowed(self):
        await self._start_mock_server({"allowed": True, "remaining": 9, "status": "ok"})
        client = RustLimiterClient(socket_path=self.sock_path)
        res = await client.check_async(scope="api", key="user1", limit=10, window_seconds=60)
        self.assertEqual(res.get("allowed"), True)
        self.assertEqual(res.get("remaining"), 9)

    async def test_check_async_socket_missing_graceful_fail(self):
        missing_sock = os.path.join(self.temp_dir.name, "missing.sock")
        client = RustLimiterClient(socket_path=missing_sock)
        res = await client.check_async(scope="api", key="user1", limit=10, window_seconds=60)
        self.assertEqual(res.get("status"), "error")

    def test_check_sync_allowed(self):
        # Inicia mock server síncrono em thread separada
        sync_sock_path = os.path.join(self.temp_dir.name, "sync_limiter.sock")
        server_ready = threading.Event()
        shutdown = threading.Event()

        def sync_server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.bind(sync_sock_path)
                s.listen(1)
                s.settimeout(1.0)
                server_ready.set()
                try:
                    conn, _ = s.accept()
                    with conn:
                        data = conn.recv(1024)
                        if data:
                            conn.sendall(json.dumps({"allowed": True, "remaining": 5, "status": "ok"}).encode("utf-8") + b"\n")
                except socket.timeout:
                    pass

        t = threading.Thread(target=sync_server)
        t.start()
        server_ready.wait()

        client = RustLimiterClient(socket_path=sync_sock_path)
        res = client.check(scope="api", key="user_sync", limit=10, window_seconds=60)
        t.join(timeout=2.0)
        self.assertEqual(res.get("allowed"), True)
        self.assertEqual(res.get("remaining"), 5)
