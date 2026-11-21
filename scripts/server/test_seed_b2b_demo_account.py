"""B2B-31: identidades reais no runtime; fixtures somente no PostgreSQL efêmero do CI."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

try:
    import asyncpg
except ImportError:
    asyncpg = None

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("b2b_demo_seed", ROOT / "scripts/maintenance/seed_b2b_demo_account.py")
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)
TEST_DSN = os.environ.get("TEST_POSTGRES_URL")
ACCOUNTS = dict(zip((seed.OWNER_EMAIL, *seed.PATIENT_EMAILS), ("demo-owner", "real-one", "real-two")))


class DemoIdentityTest(unittest.TestCase):
    def test_resolves_existing_emails_without_any_auth_mutation(self):
        import firebase_admin
        from firebase_admin import auth

        users = [SimpleNamespace(uid=uid, email=email, disabled=False) for email, uid in ACCOUNTS.items()]
        app = SimpleNamespace(project_id=seed.PROJECT_ID)
        with patch.dict(os.environ, {}, clear=True), patch.object(firebase_admin, "get_app", return_value=app), \
                patch.object(auth, "get_user_by_email", side_effect=users) as lookup, \
                patch.object(auth, "create_user") as create, patch.object(auth, "update_user") as update:
            self.assertEqual(seed.resolve_accounts(), ACCOUNTS)
            self.assertEqual([c.args[0] for c in lookup.call_args_list], list(ACCOUNTS))
            create.assert_not_called()
            update.assert_not_called()

    def test_missing_disabled_or_mismatched_identity_aborts(self):
        import firebase_admin
        from firebase_admin import auth

        for result in (
            auth.UserNotFoundError("missing"),
            SimpleNamespace(uid="one", email=seed.OWNER_EMAIL, disabled=True),
            SimpleNamespace(uid="one", email="someone-else@example.test", disabled=False),
        ):
            with self.subTest(result=type(result).__name__), patch.dict(os.environ, {}, clear=True), \
                    patch.object(firebase_admin, "get_app", return_value=SimpleNamespace(project_id=seed.PROJECT_ID)), \
                    patch.object(auth, "get_user_by_email") as lookup:
                if isinstance(result, Exception):
                    lookup.side_effect = result
                else:
                    lookup.return_value = result
                with self.assertRaises(seed.SeedError):
                    seed.resolve_accounts()

    def test_wrong_project_and_emulator_rejected_before_lookup(self):
        import firebase_admin
        from firebase_admin import auth

        for project, env in (("other-project", {}), (seed.PROJECT_ID, {"FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099"})):
            with self.subTest(project=project), patch.dict(os.environ, env, clear=True), \
                    patch.object(firebase_admin, "get_app", return_value=SimpleNamespace(project_id=project)), \
                    patch.object(auth, "get_user_by_email") as lookup:
                with self.assertRaises(seed.SeedError):
                    seed.resolve_accounts()
                lookup.assert_not_called()

    def test_cli_failure_does_not_expose_provider_response(self):
        from io import StringIO
        from unittest.mock import AsyncMock

        output = StringIO()
        with patch.object(seed, "execute", new=AsyncMock(side_effect=RuntimeError("sensitive-response"))), \
                patch("sys.stderr", output):
            self.assertEqual(seed.main([]), 1)
        self.assertNotIn("sensitive-response", output.getvalue())

    def test_reset_and_check_are_mutually_exclusive(self):
        with patch("sys.stderr", MagicMock()), self.assertRaises(SystemExit) as exc:
            seed.main(["--reset", "--check"])
        self.assertEqual(exc.exception.code, 2)


@unittest.skipUnless(TEST_DSN and asyncpg, "PostgreSQL efêmero exclusivo do CI")
class DemoSeedPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlparse(TEST_DSN)
        if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("Somente banco efêmero do CI")
        self.schema = "b2b_demo_" + uuid4().hex
        self.conn = await asyncpg.connect(TEST_DSN)
        self.addAsyncCleanup(self.cleanup)
        await self.conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" SCHEMA public')
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.conn.execute(f'SET search_path TO "{self.schema}", public')
        await self.conn.execute((ROOT / "scripts/docker/init_schema.sql").read_text(encoding="utf-8-sig"))
        for number in ("001", "053", "080", "081", "084", "090", "091", "092", "092b", "094", "095"):
            path = next(p for p in (ROOT / "migrations").glob(f"{number}_*.sql") if not p.stem.endswith("_rollback"))
            async with self.conn.transaction():
                await self.conn.execute(path.read_text(encoding="utf-8-sig"))
        await self.conn.execute(
            "INSERT INTO profiles(id,full_name,user_role,height,weight,basic_profile_complete,self_care_complete) VALUES "
            "('real-one','Perfil pessoal um','PATIENT',170,70,true,true),"
            "('real-two','Perfil pessoal dois','PATIENT',180,80,true,true),"
            "('other-owner','Outro programa',NULL,NULL,NULL,false,false)"
        )
        self.other = await self.conn.fetchval(
            "INSERT INTO partner_programs(owner_user_id,program_name,company_name,invite_code) "
            "VALUES('other-owner','Outro','Outro','OTHER-1') RETURNING id"
        )
        # Mesmo paciente em outro tenant: prova escopo por programa, não por UID.
        await seed.seed_participant(self.conn, self.other, "other-owner", "real-one", full=True, family_share=False)
        await self.conn.execute(
            "INSERT INTO medications(user_id,name,schedule_time) VALUES('real-one','Registro existente','08:00')"
        )
        await self.conn.execute(
            "INSERT INTO consultations(user_id,title,scheduled_date) VALUES('real-one','Consulta pessoal',now())"
        )
        self.personal_before = await self.personal_snapshot()
        self.other_before = await self.program_snapshot(self.other)

    async def cleanup(self):
        await self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        await self.conn.close()

    async def personal_snapshot(self):
        return [await self.conn.fetchval(
            f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb)::text FROM {table} t "
            + ("WHERE id IN ('real-one','real-two')" if table == "profiles" else "")
        ) for table in ("profiles", "medications", "medication_logs", "consultations", "family_bindings")]

    async def program_snapshot(self, program_id):
        result = []
        for table in ("program_enrollments", "program_reminders", "program_appointments",
                      "program_direct_threads", "program_direct_messages"):
            result.append(await self.conn.fetchval(
                f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text "
                f"FROM {table} t WHERE program_id=$1", program_id,
            ))
        result.append(await self.conn.fetchval(
            "SELECT jsonb_agg(to_jsonb(rc) ORDER BY rc.reminder_id)::text FROM program_reminder_receipts rc "
            "JOIN program_reminders r ON r.id=rc.reminder_id WHERE r.program_id=$1", program_id,
        ))
        return result

    async def test_complete_seed_and_rerun_preserve_real_profiles_and_usage(self):
        first = await seed.run(self.conn, ACCOUNTS)
        program = await self.conn.fetchrow("SELECT * FROM partner_programs WHERE invite_code=$1", seed.INVITE_CODE)
        self.assertEqual(first["metrics_added"], {"daily_metrics": 3, "vital_signs": 3})
        self.assertEqual(program["approval_status"], "APPROVED")
        self.assertEqual(program["seat_limit"], 50)
        self.assertEqual(program["primary_color"], "#00695C")
        self.assertEqual(program["company_name"], "Horizonte")
        self.assertEqual(program["program_name"], "Horizonte Cuida")
        self.assertIsNone(program["logo_url"])
        config = json.loads(program["experience_config"])
        self.assertEqual(config["features"], dict.fromkeys(seed.FEATURES, True))
        self.assertTrue(config["ai_receptionist_policy"]["partner_faq_knowledge"])
        owner = await self.conn.fetchrow("SELECT * FROM profiles WHERE id='demo-owner'")
        self.assertEqual(owner["account_mode"], "BUSINESS_ONLY")
        self.assertIsNone(owner["user_role"])
        self.assertFalse(owner["self_care_complete"])
        rows = await self.conn.fetch(
            "SELECT * FROM program_enrollments WHERE program_id=$1 ORDER BY patient_user_id", program["id"],
        )
        self.assertEqual([json.loads(r["shared_categories"]) for r in rows], [seed.METRICS, seed.METRICS[:2]])
        self.assertFalse(rows[0]["family_share_enabled"])
        appointments = await self.conn.fetch(
            "SELECT status,scheduled_at>now() AS future FROM program_appointments WHERE program_id=$1 "
            "ORDER BY patient_user_id", program["id"],
        )
        self.assertEqual([r["status"] for r in appointments], ["PENDING", "CONFIRMED"])
        self.assertTrue(appointments[1]["future"])
        self.assertEqual(await self.conn.fetchval(
            "SELECT count(*) FROM program_reminder_receipts rc JOIN program_reminders r ON r.id=rc.reminder_id "
            "WHERE r.program_id=$1", program["id"],
        ), 2)
        self.assertEqual([r["handling_mode"] for r in await self.conn.fetch(
            "SELECT handling_mode FROM program_direct_threads WHERE program_id=$1 ORDER BY patient_user_id", program["id"],
        )], ["WAITING_HUMAN", "AI_ACTIVE"])
        await self.conn.execute(
            "UPDATE program_direct_threads SET handling_mode='HUMAN_ACTIVE' WHERE program_id=$1", program["id"],
        )
        await self.conn.execute(
            "UPDATE partner_programs SET logo_url='https://bragasaude.online/images/logo.png' WHERE id=$1", program["id"],
        )
        second = await seed.run(self.conn, ACCOUNTS)
        self.assertIsNone(await self.conn.fetchval("SELECT logo_url FROM partner_programs WHERE id=$1", program["id"]))
        await self.conn.execute("UPDATE partner_programs SET logo_url='https://example.test/horizonte.png' WHERE id=$1", program["id"])
        await seed.run(self.conn, ACCOUNTS)
        self.assertEqual(await self.conn.fetchval("SELECT logo_url FROM partner_programs WHERE id=$1", program["id"]),
                         "https://example.test/horizonte.png")
        self.assertEqual(first["program_id"], second["program_id"])
        self.assertEqual(second["metrics_added"], {"daily_metrics": 0, "vital_signs": 0})
        self.assertEqual(await self.conn.fetchval(
            "SELECT count(*) FROM program_direct_messages WHERE program_id=$1", program["id"],
        ), 3)
        self.assertEqual(await self.conn.fetchval(
            "SELECT count(*) FROM program_direct_threads WHERE program_id=$1 AND handling_mode='HUMAN_ACTIVE'",
            program["id"],
        ), 2)
        self.assertEqual(await self.personal_snapshot(), self.personal_before)
        self.assertEqual(await self.program_snapshot(self.other), self.other_before)

    async def test_recent_measurements_not_overwritten_or_supplemented(self):
        await self.conn.execute("INSERT INTO daily_metrics(user_id,date,steps) VALUES('real-one',CURRENT_DATE,12345)")
        await self.conn.execute(
            "INSERT INTO vital_signs(user_id,hydration_ml,measured_at) VALUES('real-one',333,now())"
        )
        before = [await self.conn.fetchval(f"SELECT jsonb_agg(to_jsonb(t))::text FROM {table} t")
                  for table in ("daily_metrics", "vital_signs", "biometrics")]
        result = await seed.run(self.conn, ACCOUNTS)
        self.assertEqual(result["metrics_added"], {"daily_metrics": 0, "vital_signs": 0})
        after = [await self.conn.fetchval(f"SELECT jsonb_agg(to_jsonb(t))::text FROM {table} t")
                 for table in ("daily_metrics", "vital_signs", "biometrics")]
        self.assertEqual(before, after)

    async def test_active_family_binding_only_in_authorized_direction(self):
        await self.conn.execute(
            "INSERT INTO family_bindings(patient_id,caregiver_id,status,caregiver_name,caregiver_relation,"
            "connection_code,expires_at) VALUES('real-one','real-two','ACTIVE','Familiar','Teste',"
            "'FAMILY-DEMO',now()+interval '1 day')"
        )
        self.assertTrue((await seed.run(self.conn, ACCOUNTS))["family_share_enabled"])
        await self.conn.execute("UPDATE family_bindings SET status='REVOKED'")
        self.assertFalse((await seed.run(self.conn, ACCOUNTS))["family_share_enabled"])

    async def test_reset_preserves_personal_history_program_audit_and_other_tenant(self):
        await seed.run(self.conn, ACCOUNTS)
        metrics = [await self.conn.fetchval(f"SELECT jsonb_agg(to_jsonb(t))::text FROM {table} t")
                   for table in ("daily_metrics", "vital_signs", "vital_sign_changes")]
        result = await seed.run(self.conn, ACCOUNTS, reset=True)
        self.assertEqual(result["deleted"], {"program_direct_messages": 3, "program_direct_threads": 2,
                         "program_appointments": 2, "program_reminders": 2, "program_enrollments": 2})
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM partner_programs"), 2)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM program_reminder_receipts"), 1)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM program_audit_logs"), 2)
        self.assertEqual(await self.personal_snapshot(), self.personal_before)
        self.assertEqual(await self.program_snapshot(self.other), self.other_before)
        self.assertEqual(metrics, [await self.conn.fetchval(f"SELECT jsonb_agg(to_jsonb(t))::text FROM {table} t")
                                  for table in ("daily_metrics", "vital_signs", "vital_sign_changes")])
        self.assertTrue(all(n == 0 for n in (await seed.run(self.conn, ACCOUNTS, reset=True))["deleted"].values()))
        self.assertEqual((await seed.run(self.conn, ACCOUNTS))["metrics_added"], {"daily_metrics": 0, "vital_signs": 0})

    async def test_check_and_absent_reset_do_not_create_anything(self):
        result = await seed.run(self.conn, ACCOUNTS, check=True)
        self.assertFalse(result["program_exists"])
        self.assertFalse((await seed.run(self.conn, ACCOUNTS, reset=True))["program_exists"])
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM profiles"), 3)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM partner_programs"), 1)

    async def test_missing_patient_profile_aborts_before_owner_creation(self):
        await self.conn.execute("DELETE FROM profiles WHERE id='real-two'")
        with self.assertRaises(seed.SeedError):
            await seed.run(self.conn, ACCOUNTS)
        self.assertFalse(await self.conn.fetchval("SELECT EXISTS(SELECT 1 FROM profiles WHERE id='demo-owner')"))

    async def test_program_collision_aborts_seed_and_reset(self):
        await self.conn.execute("UPDATE partner_programs SET invite_code=$1 WHERE id=$2", seed.INVITE_CODE, self.other)
        for reset in (False, True):
            with self.assertRaises(seed.SeedError):
                await seed.run(self.conn, ACCOUNTS, reset=reset)
        self.assertEqual(await self.program_snapshot(self.other), self.other_before)

    async def test_failure_late_in_seed_rolls_back_every_write(self):
        from unittest.mock import AsyncMock

        with patch.object(seed, "supplement_metrics", new=AsyncMock(side_effect=RuntimeError("failure"))):
            with self.assertRaises(RuntimeError):
                await seed.run(self.conn, ACCOUNTS)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM partner_programs"), 1)
        self.assertFalse(await self.conn.fetchval("SELECT EXISTS(SELECT 1 FROM profiles WHERE id='demo-owner')"))
        self.assertEqual(await self.personal_snapshot(), self.personal_before)
        self.assertEqual(await self.program_snapshot(self.other), self.other_before)


if __name__ == "__main__":
    unittest.main()
