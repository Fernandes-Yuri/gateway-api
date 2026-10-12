"""Testes automatizados de conformidade para Higiene Critica Fase B.

Valida:
1. Migration 018 (schema de device_push_tokens e formalizacao das tabelas previamente inline).
2. Metodos assincronos relacionais em core/devices.py (register_device_token, get_active_tokens_for_caregivers, get_active_devices_for_users, migrate_legacy_json_devices).
3. Ausencia de DDL inline no startup() e remocao de _proactive_display_name.
4. Consulta unica agregada no scheduled_proactive_job.
5. Gatekeeper lint-and-test no workflow de deploy do GitHub Actions.
"""
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Garante scripts/server no path caso executado diretamente
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from unittest.mock import AsyncMock, MagicMock, patch

import server
from core import devices


class TestMigration018(unittest.TestCase):
    def test_migration_018_structure(self):
        root_dir = Path(__file__).resolve().parent.parent.parent
        migration_file = root_dir / "migrations" / "018_device_push_tokens_and_audit_schemas.sql"
        self.assertTrue(migration_file.exists(), "Migration 018 deve existir no diretorio migrations/")

        content = migration_file.read_text(encoding="utf-8")
        self.assertIn("device_push_tokens", content)
        self.assertIn("auth_verification_codes", content)
        self.assertIn("audit_logs", content)
        self.assertIn("assistant_preferences", content)
        self.assertIn("assistant_preference_pending", content)
        self.assertIn("lock_timeout = '10s'", content)
        self.assertIn("018_device_push_tokens_and_audit_schemas", content)


class TestCoreDevicesRelational(unittest.IsolatedAsyncioTestCase):
    def make_mock_pool(self, fetch_return=None):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetch = AsyncMock(return_value=fetch_return or [])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        return pool, conn

    async def test_register_device_token_upsert(self):
        pool, conn = self.make_mock_pool()
        ok = await devices.register_device_token(
            pool=pool,
            user_id="user-123",
            token="fcm-token-abc",
            role="caregiver",
            platform="fcm",
        )
        self.assertTrue(ok)
        conn.execute.assert_called_once()
        sql, token, uid, role, plat = conn.execute.call_args[0]
        self.assertIn("ON CONFLICT (token) DO UPDATE", sql)
        self.assertEqual(token, "fcm-token-abc")
        self.assertEqual(uid, "user-123")
        self.assertEqual(role, "caregiver")
        self.assertEqual(plat, "fcm")

    async def test_register_device_token_none_pool(self):
        ok = await devices.register_device_token(None, "user-123", "token-xyz")
        self.assertFalse(ok)

    async def test_get_active_tokens_for_caregivers(self):
        pool, conn = self.make_mock_pool(fetch_return=[
            {"token": "token-cg-1"},
            {"token": "token-cg-2"},
        ])
        tokens = await devices.get_active_tokens_for_caregivers(pool, exclude_user_id="user-author")
        self.assertEqual(tokens, ["token-cg-1", "token-cg-2"])
        sql = conn.fetch.call_args[0][0]
        self.assertIn("LOWER(role) = 'caregiver'", sql)
        self.assertIn("user_id != $1", sql)

    async def test_get_active_tokens_no_pool(self):
        tokens = await devices.get_active_tokens_for_caregivers(None)
        self.assertEqual(tokens, [])

    async def test_get_active_devices_for_users(self):
        pool, conn = self.make_mock_pool(fetch_return=[
            {"token": "t1", "user_id": "u1", "role": "USER", "platform": "android"},
        ])
        devs = await devices.get_active_devices_for_users(pool)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["token"], "t1")

    async def test_migrate_legacy_json_devices(self):
        pool, conn = self.make_mock_pool()
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_file = os.path.join(tmpdir, "registered_devices.json")
            sample_data = {
                "user_cg": {"token": "token-cg", "role": "CAREGIVER", "platform": "fcm"},
                "user_pt": {"token": "token-pt", "role": "USER", "platform": "android"},
            }
            with open(legacy_file, "w", encoding="utf-8") as f:
                json.dump(sample_data, f)

            with patch.object(devices, "DEVICES_PATH", legacy_file):
                count = await devices.migrate_legacy_json_devices(pool)

            self.assertEqual(count, 2)
            self.assertFalse(os.path.exists(legacy_file), "Arquivo original deve ter sido renomeado")
            self.assertTrue(os.path.exists(f"{legacy_file}.migrated"), "Arquivo .migrated de backup deve existir")


class TestServerStartupAndProactiveCleanliness(unittest.IsolatedAsyncioTestCase):
    def test_no_inline_ddl_in_startup(self):
        source = inspect.getsource(server.startup)
        self.assertNotIn("CREATE TABLE", source, "startup() nao deve conter CREATE TABLE inline")
        self.assertNotIn("PREFERENCE_SCHEMA", source, "startup() nao deve executar PREFERENCE_SCHEMA inline")
        self.assertIn("SELECT 1;", source, "startup() deve usar SELECT 1; como ping leve")
        self.assertIn("migrate_legacy_json_devices", source, "startup() deve chamar migrate_legacy_json_devices")

    def test_proactive_display_name_deleted(self):
        self.assertFalse(hasattr(server, "_proactive_display_name"), "_proactive_display_name() deve ter sido deletada")

    async def test_scheduled_proactive_job_single_query(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"token": "token-1", "user_id": "u1", "full_name": "Maria Silva"},
        ])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.object(server, "db_pool", pool), \
                patch.object(server, "dispatch_push", MagicMock(return_value=True)) as mock_push:
            await server.scheduled_proactive_job("manha")

        conn.fetch.assert_called_once()
        sql = conn.fetch.call_args[0][0]
        self.assertIn("SELECT d.token, d.user_id, p.full_name", sql)
        self.assertIn("JOIN profiles p", sql)
        self.assertIn("NOT EXISTS", sql)
        mock_push.assert_called_once()
        self.assertEqual(mock_push.call_args.kwargs.get("token"), "token-1")
        self.assertIn("Maria", mock_push.call_args.kwargs.get("body"))


class TestDeployWorkflow(unittest.TestCase):
    def test_gatekeeper_present_in_workflow(self):
        root_dir = Path(__file__).resolve().parent.parent.parent
        workflow_file = root_dir / ".github" / "workflows" / "deploy.yml"
        self.assertTrue(workflow_file.exists())
        content = workflow_file.read_text(encoding="utf-8")

        self.assertIn("lint-and-test:", content)
        self.assertIn("ruff check scripts/server/", content)
        self.assertIn("python -m unittest discover", content)
        # O deploy precisa depender dos gates (forma escalar ou de lista).
        linha_needs = next(
            (l.strip() for l in content.splitlines()
             if l.strip().startswith("needs:")), "")
        self.assertIn("lint-and-test", linha_needs)
        self.assertIn("ensaio-migracoes", linha_needs)


if __name__ == "__main__":
    unittest.main()
