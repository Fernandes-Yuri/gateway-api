"""Testa o repasse do pool Postgres aos routers extraidos (Fase A).

Contexto: os routers capturam db_pool via configure_state no momento do
registro (import time), quando o pool ainda nao existe — ele so nasce no
startup(). Se o startup nao repassar, os routers ficam com None para sempre
mesmo em producao, e toda rota de banco devolve 503/offline_mode.

Cobertura: simula create_pool com um pool falso e verifica que todos os
_POOL_ROUTERS recebem o pool vivo, e None de volta apos shutdown.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server


class PoolPropagationTest(unittest.TestCase):
    def _run_lifecycle(self, after_startup=None):
        """Roda startup, chama after_startup (se houver) e fecha com shutdown."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(server.startup())
            if after_startup is not None:
                after_startup()
            loop.run_until_complete(server.shutdown())
        finally:
            loop.close()

    def test_startup_propagates_live_pool_to_all_routers(self):
        fake_pool = MagicMock()
        fake_pool.acquire.return_value.__aenter__ = AsyncMock()
        fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_pool.close = AsyncMock()

        original = server.db_pool
        captured = {}

        def _snapshot():
            # Captura o estado de cada router LOGO APOS o startup, antes do
            # shutdown zerar. E o unico momento em que o pool vivo existe.
            captured['routers'] = [(r, r._state.db_pool) for r in server._POOL_ROUTERS]

        try:
            with patch.object(server, "HAS_POSTGRES", True), \
                 patch.object(server, "DATABASE_URL", "postgresql://u:p@h/db"), \
                 patch("asyncpg.create_pool", AsyncMock(return_value=fake_pool)), \
                 patch.object(server, "HAS_SCHEDULER", False), \
                 patch.object(server, "HAS_WA_LINK", False):
                self._run_lifecycle(after_startup=_snapshot)

                for router, pool_seen in captured['routers']:
                    self.assertIs(
                        pool_seen, fake_pool,
                        f"{router.__name__} nao recebeu o pool no startup")
        finally:
            server.db_pool = original
            for router in server._POOL_ROUTERS:
                router._state.db_pool = None

    def test_shutdown_returns_routers_to_none(self):
        fake_pool = MagicMock()
        fake_pool.acquire.return_value.__aenter__ = AsyncMock()
        fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_pool.close = AsyncMock()

        original = server.db_pool
        try:
            with patch.object(server, "HAS_POSTGRES", True), \
                 patch.object(server, "DATABASE_URL", "postgresql://u:p@h/db"), \
                 patch("asyncpg.create_pool", AsyncMock(return_value=fake_pool)), \
                 patch.object(server, "HAS_SCHEDULER", False), \
                 patch.object(server, "HAS_WA_LINK", False):
                self._run_lifecycle()
                # Apos shutdown + close, todos voltam a None.
                for router in server._POOL_ROUTERS:
                    self.assertIsNone(
                        router._state.db_pool,
                        f"{router.__name__} nao voltou a None apos shutdown")
        finally:
            server.db_pool = original
            for router in server._POOL_ROUTERS:
                router._state.db_pool = None


if __name__ == "__main__":
    unittest.main()
