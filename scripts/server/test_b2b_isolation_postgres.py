"""Testes B2B-13 & B2B-15: Isolamento multi-tenant, motor No-Code (091), LGPD delete_account e retrocompatibilidade v1.9.6."""
import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

try:
    import asyncpg
except ImportError:
    asyncpg = None

try:
    from services import account_deletion
except ImportError:
    account_deletion = None

ROOT = Path(__file__).resolve().parents[2]
TEST_DSN = os.getenv("TEST_POSTGRES_URL")
B2B_RUST_PATH = ROOT / "rust/crates/braga-edge/src/b2b.rs"
B2B_ENGAGEMENT_RUST_PATH = ROOT / "rust/crates/braga-edge/src/b2b_engagement.rs"
B2B_FAMILY_LINK_RUST_PATH = ROOT / "rust/crates/braga-edge/src/b2b_family_link.rs"
B2B_KYB_RUST_PATH = ROOT / "rust/crates/braga-edge/src/b2b_kyb.rs"
MIG_091_PATH = ROOT / "migrations/091_b2b_nocode_experience_builder.sql"
MIG_091_ROLLBACK_PATH = ROOT / "migrations/091_b2b_nocode_experience_builder_rollback.sql"
MIG_091B_PATH = ROOT / "migrations/091b_b2b_auto_kyb.sql"
MIG_091B_ROLLBACK_PATH = ROOT / "migrations/091b_b2b_auto_kyb_rollback.sql"
MIG_092_PATH = ROOT / "migrations/092_b2b_reminders_appointments_direct_chat.sql"
MIG_092_ROLLBACK_PATH = (
    ROOT / "migrations/092_b2b_reminders_appointments_direct_chat_rollback.sql"
)
MIG_092B_PATH = ROOT / "migrations/092b_b2b_business_session_mode.sql"
MIG_092B_ROLLBACK_PATH = (
    ROOT / "migrations/092b_b2b_business_session_mode_rollback.sql"
)
MIG_093_PATH = ROOT / "migrations/093_b2b_owner_account_mode_backfill.sql"
MIG_093_ROLLBACK_PATH = (
    ROOT / "migrations/093_b2b_owner_account_mode_backfill_rollback.sql"
)


class B2BStaticGuardsTest(unittest.TestCase):
    """Checagens estaticas defensivas sem necessidade de servidor externo."""

    def test_rust_engine_enforces_rds_limits_and_zero_file_egress(self):
        src = B2B_RUST_PATH.read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE", src)
        self.assertIn(".clamp(1, 50)", src)
        self.assertIn(".clamp(1, 90)", src)
        self.assertIn(".clamp(1, 200)", src)
        self.assertIn("LIMIT 31", src)
        self.assertIn("LIMIT 30", src)
        self.assertIn("guard_program_module", src)
        self.assertIn("intersect_effective_categories", src)
        self.assertIn("validate_and_normalize_experience_config", src)

        forbidden_tables = re.compile(
            r"\bFROM\s+(exams|exam_items|exam_cloud_consents|symptoms_diary|family_messages)\b",
            re.IGNORECASE,
        )
        self.assertIsNone(
            forbidden_tables.search(src),
            "O motor B2B em Rust nunca deve consultar exams, symptoms_diary ou family_messages.",
        )

        eng_src = B2B_ENGAGEMENT_RUST_PATH.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(
                r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(medications|medication_logs|consultations|family_messages)\b",
                eng_src,
                re.IGNORECASE,
            ),
            "Modulo de engajamento B2B nunca pode escrever em medications, consultations ou family_messages.",
        )

        fam_src = B2B_FAMILY_LINK_RUST_PATH.read_text(encoding="utf-8")
        self.assertIn("require_family_access", fam_src)
        self.assertIn("is_family_link_authorized", fam_src)
        self.assertIsNone(
            re.search(
                r"\bFROM\s+(program_direct_messages|program_direct_threads|family_messages|exams|exam_items|symptoms_diary)\b",
                fam_src,
                re.IGNORECASE,
            ),
            "Visao familiar B2B-21 nunca pode expor chat 1:1 B2B nem chat/exames pessoais.",
        )

    def test_migration_092_is_additive_reversible_and_isolated(self):
        sql_up = MIG_092_PATH.read_text(encoding="utf-8")
        sql_down = MIG_092_ROLLBACK_PATH.read_text(encoding="utf-8")
        self.assertIn("SET LOCAL lock_timeout = '5s';", sql_up)
        self.assertIn("SET LOCAL statement_timeout = '60s';", sql_up)
        for tbl in (
            "program_reminders",
            "program_reminder_receipts",
            "program_appointments",
            "program_direct_threads",
            "program_direct_messages",
            "program_abuse_reports",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {tbl}", sql_up)
            self.assertIn(f"DROP TABLE IF EXISTS {tbl};", sql_down)
        for kind in (
            "PARTNER_REMINDER",
            "PARTNER_APPOINTMENT",
            "PARTNER_DIRECT_MESSAGE",
        ):
            self.assertIn(kind, sql_up)

    def test_migration_091_is_additive_reversible_and_unblocks_lgpd_deletion(self):
        sql_up = MIG_091_PATH.read_text(encoding="utf-8")
        sql_down = MIG_091_ROLLBACK_PATH.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS template_key", sql_up)
        self.assertIn("ADD COLUMN IF NOT EXISTS experience_config", sql_up)
        self.assertIn("ON DELETE CASCADE", sql_up)
        self.assertIn("trg_profiles_b2b_lgpd_cleanup", sql_up)
        self.assertIn("DROP TRIGGER IF EXISTS trg_profiles_b2b_lgpd_cleanup", sql_down)
        self.assertIn("DROP COLUMN IF EXISTS experience_config", sql_down)

        for number in (3, 10, 17, 18, 31, 32, 33, 53, 54, 56, 71, 80, 81, 82, 90, 91):
            matches = [
                f
                for f in (ROOT / "migrations").glob(f"{number:03d}_*.sql")
                if not f.stem.endswith("_rollback")
            ]
            self.assertEqual(len(matches), 1, f"Migration {number:03d} nao encontrada")

    def test_migration_091b_is_additive_reversible_and_protects_cnpj_and_kyb(self):
        sql_up = MIG_091B_PATH.read_text(encoding="utf-8")
        sql_down = MIG_091B_ROLLBACK_PATH.read_text(encoding="utf-8")
        self.assertIn("uq_partner_programs_active_cnpj", sql_up)
        self.assertIn("CREATE TABLE IF NOT EXISTS cnpj_lookup_cache", sql_up)
        self.assertIn("B2B_PROGRAM_APPROVED", sql_up)
        self.assertIn("b2b_r2_keys", sql_up)
        self.assertIn("DROP INDEX IF EXISTS uq_partner_programs_active_cnpj", sql_down)
        self.assertIn("DROP TABLE IF EXISTS cnpj_lookup_cache", sql_down)
        self.assertIn("DROP COLUMN IF EXISTS b2b_r2_keys", sql_down)

        kyb_src = B2B_KYB_RUST_PATH.read_text(encoding="utf-8")
        self.assertIn("normalize_and_hash_cpf", kyb_src)
        self.assertIn("brasilapi.com.br", kyb_src)
        self.assertIn("publica.cnpj.ws", kyb_src)
        self.assertIn("receitaws.com.br", kyb_src)
        self.assertNotIn("responsible_cpf varchar", sql_up)

    def test_migration_092b_business_session_mode_is_additive_reversible(self):
        sql_up = MIG_092B_PATH.read_text(encoding="utf-8")
        sql_down = MIG_092B_ROLLBACK_PATH.read_text(encoding="utf-8")
        sql_up_code = "\n".join(
            line for line in sql_up.splitlines() if not line.lstrip().startswith("--")
        )
        self.assertIn("SET LOCAL lock_timeout = '5s';", sql_up)
        self.assertIn("ADD COLUMN IF NOT EXISTS account_mode", sql_up)
        self.assertIn("DEFAULT 'PERSONAL'", sql_up)
        self.assertIn("profiles_account_mode_check", sql_up)
        self.assertIn("'BUSINESS_ONLY'", sql_up)
        self.assertIn("'DUAL'", sql_up)
        self.assertNotIn("user_role", sql_up_code)
        self.assertNotIn("profiles_completion_check", sql_up_code)
        self.assertIn("DROP CONSTRAINT IF EXISTS profiles_account_mode_check", sql_down)
        self.assertIn("DROP COLUMN IF EXISTS account_mode", sql_down)

        b2b_src = B2B_RUST_PATH.read_text(encoding="utf-8")
        self.assertIn("update_account_mode_handler", b2b_src)
        self.assertIn("default_session_mode", b2b_src)
        self.assertIn('"accountMode"', b2b_src)
        self.assertIn("is_account_mode_transition_allowed", b2b_src)

        sync_src = (ROOT / "scripts/server/routers/sync.py").read_text(encoding="utf-8")
        self.assertIn('"accountMode"', sync_src)

    def test_migration_093_owner_account_mode_backfill_is_idempotent_and_reversible(self):
        sql_up = MIG_093_PATH.read_text(encoding="utf-8")
        sql_down = MIG_093_ROLLBACK_PATH.read_text(encoding="utf-8")
        self.assertIn("SET LOCAL lock_timeout = '5s';", sql_up)
        self.assertIn("SET LOCAL statement_timeout = '60s';", sql_up)
        self.assertIn("account_mode = 'DUAL'", sql_up)
        self.assertIn("account_mode = 'PERSONAL'", sql_up)
        self.assertIn("owner_user_id", sql_up)
        self.assertIn("closed_at IS NULL", sql_up)
        self.assertIn("updated_at = NOW()", sql_up)
        # Backfill de dados: nao altera schema nem o contrato de onboarding.
        self.assertNotIn("ALTER TABLE", sql_up)
        self.assertNotIn("user_role", sql_up)
        self.assertNotIn("profiles_completion_check", sql_up)
        self.assertIn("account_mode = 'PERSONAL'", sql_down)
        self.assertIn("account_mode = 'DUAL'", sql_down)

    def test_b2b_r2_key_extraction_email_templates_and_internal_notify(self):
        import asyncio
        from types import SimpleNamespace
        try:
            import email_service
            from routers import admin
        except ImportError as exc:
            self.skipTest(f"Dependencias FastAPI ausentes neste Python local: {exc}")

        if account_deletion is not None:
            keys = account_deletion._extract_b2b_r2_keys_for_program(
                {
                    "id": "prog-1",
                    "logo_url": "https://api.bragasaude.online/api/files/b2b-logos/prog-1/logo-abc.png?exp=9&sig=x",
                    "proof_documents": [
                        {"id": "p1", "storageKey": "b2b-kyb/prog-1/p1.pdf"},
                        {"id": "evil", "storageKey": "../outside/secret.pdf"},
                    ],
                }
            )
            self.assertEqual(
                keys,
                ["b2b-logos/prog-1/logo-abc.png", "b2b-kyb/prog-1/p1.pdf"],
            )

        sent = []

        def fake_send(to_email, subject, html_body, plain_text):
            sent.append((to_email, subject, html_body, plain_text))
            return True

        with patch.object(email_service, "_send_gmail_sync", side_effect=fake_send):
            res = asyncio.run(
                email_service.send_b2b_decision_email(
                    to_email="clinic@example.test",
                    program_name="Clínica <Vida>",
                    decision="APPROVED",
                    invite_code="VIDA-1234",
                )
            )
            self.assertTrue(res["success"])
            self.assertIn("aprovado", sent[0][1])
            self.assertIn("VIDA-1234", sent[0][2])
            self.assertIn("Clínica &lt;Vida&gt;", sent[0][2])

            body = admin.B2BDecisionEmailRequest(
                ownerUserId="owner-1",
                programName="Clínica Vida",
                decision="CHANGES_REQUESTED",
                note="Envie comprovante CRM.",
            )
            with patch(
                "firebase_admin.auth.get_user",
                return_value=SimpleNamespace(email="owner@example.test"),
            ):
                notify_res = asyncio.run(
                    admin.notify_b2b_decision_internal(body, _auth=True)
                )
            self.assertTrue(notify_res["success"])
            self.assertEqual(sent[1][0], "owner@example.test")
            self.assertIn("Envie comprovante CRM.", sent[1][2])


@unittest.skipUnless(TEST_DSN and asyncpg is not None, "PostgreSQL efemero exclusivo do CI")
class B2BIsolationPostgresTest(unittest.IsolatedAsyncioTestCase):
    """Prova isolamento estrito multi-tenant, No-Code 091, delete_account LGPD e retrocompatibilidade v1.9.6 no Postgres 16."""

    async def asyncSetUp(self):
        parsed = urlparse(TEST_DSN)
        if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path != "/braga_privacy_test":
            raise RuntimeError("Este teste aceita apenas o banco efemero do CI")

        self.schema = "b2b_iso_" + uuid.uuid4().hex
        self.control = await asyncpg.connect(TEST_DSN)
        self.pool = None
        try:
            await self.control.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" SCHEMA public')
            await self.control.execute(f'CREATE SCHEMA "{self.schema}"')

            async def setup(conn):
                await conn.execute(f'SET search_path TO "{self.schema}", public')

            self.pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2, setup=setup)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    (ROOT / "scripts/docker/init_schema.sql").read_text(encoding="utf-8-sig")
                )
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY)"
                )
                # 001 traz basic_profile_complete, self_care_complete e profiles_completion_check
                await conn.execute(
                    (ROOT / "migrations/001_profile_and_family.sql").read_text(
                        encoding="utf-8-sig"
                    )
                )
                for number in (3, 10, 17, 18, 31, 32, 33, 53, 54, 56, 71, 80, 81, 82, 90, 91):
                    mig_file = next(
                        f
                        for f in (ROOT / "migrations").glob(f"{number:03d}_*.sql")
                        if not f.stem.endswith("_rollback")
                    )
                    async with conn.transaction():
                        await conn.execute(mig_file.read_text(encoding="utf-8-sig"))
                async with conn.transaction():
                    await conn.execute(MIG_091B_PATH.read_text(encoding="utf-8-sig"))
                async with conn.transaction():
                    await conn.execute(MIG_092_PATH.read_text(encoding="utf-8-sig"))
                async with conn.transaction():
                    await conn.execute(MIG_092B_PATH.read_text(encoding="utf-8-sig"))

                await conn.execute(
                    """
                    INSERT INTO profiles (id, full_name) VALUES
                        ('owner-a', 'Responsavel Clinica A'),
                        ('owner-b', 'Responsavel Clinica B'),
                        ('owner-draft', 'Responsavel Rascunho'),
                        ('patient-1', 'Paciente Um'),
                        ('patient-2', 'Paciente Dois')
                    """
                )
                self.prog_a = await conn.fetchval(
                    """
                    INSERT INTO partner_programs
                        (owner_user_id, program_name, company_name, cnpj, approval_status, invite_code, seat_limit)
                    VALUES
                        ('owner-a', 'Programa A', 'Clinica A', '12.345.678/0001-95', 'APPROVED', 'CLINA-1111', 2)
                    RETURNING id
                    """
                )
                self.prog_b = await conn.fetchval(
                    """
                    INSERT INTO partner_programs
                        (owner_user_id, program_name, company_name, cnpj, approval_status, invite_code, seat_limit, template_key)
                    VALUES
                        ('owner-b', 'Programa B', 'Clinica B', '98.765.432/0001-10', 'APPROVED', 'CLINB-2222', 5, 'NUTRICAO_BEM_ESTAR')
                    RETURNING id
                    """
                )
                self.prog_draft = await conn.fetchval(
                    """
                    INSERT INTO partner_programs
                        (owner_user_id, program_name, company_name, approval_status, template_key)
                    VALUES
                        ('owner-draft', 'Programa Rascunho', 'Clinica Rascunho', 'DRAFT', 'CUSTOM')
                    RETURNING id
                    """
                )
                await conn.execute(
                    """
                    INSERT INTO program_enrollments
                        (program_id, patient_user_id, status, shared_categories, family_share_enabled)
                    VALUES
                        ($1, 'patient-1', 'ACTIVE', '["steps"]'::jsonb, true),
                        ($2, 'patient-2', 'ACTIVE', '["authorized_records"]'::jsonb, false)
                    """,
                    self.prog_a,
                    self.prog_b,
                )
                await conn.execute(
                    """
                    INSERT INTO program_audit_logs
                        (program_id, actor_user_id, target_user_id, action, metadata)
                    VALUES
                        ($1, 'owner-a', 'owner-a', 'PROGRAM_APPROVED', '{}'::jsonb),
                        ($2, 'owner-draft', 'owner-draft', 'PROGRAM_CREATED', '{}'::jsonb)
                    """,
                    self.prog_a,
                    self.prog_draft,
                )
                # 093 e backfill de dados: precisa rodar DEPOIS do seed de owners
                # para refletir o estado snapshot (owner com programa aberto -> DUAL).
                async with conn.transaction():
                    await conn.execute(MIG_093_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            await self.asyncTearDown()
            raise

    async def asyncTearDown(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        if self.control is not None:
            await self.control.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            await self.control.close()
            self.control = None

    async def test_strict_b2b_access_uses_live_ownership_and_both_family_keys(self):
        query = (ROOT / "rust/crates/braga-edge/src/sql/b2b_access.sql").read_text()
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO profiles (id, full_name) VALUES ('family', 'Familiar'), ('stranger', 'Estranho')")
            access = await conn.fetchrow(query, "stranger")
            self.assertFalse(any(access.values()))
            access = await conn.fetchrow(query, "patient-1")
            self.assertTrue(access["participant"])
            self.assertFalse(access["owner"])
            await conn.execute("UPDATE profiles SET account_mode = 'BUSINESS_ONLY' WHERE id = 'owner-a'")
            access = await conn.fetchrow(query, "owner-a")
            self.assertTrue(access["business_account"] and access["owner"])
            await conn.execute("""
                INSERT INTO family_bindings
                    (patient_id, caregiver_id, caregiver_name, caregiver_relation, connection_code, status, expires_at)
                VALUES ('patient-1', 'family', 'Familiar', 'Filha', 'KPI-FAM1', 'ACTIVE', NOW() + interval '7 days')
            """)
            self.assertFalse((await conn.fetchrow(query, "family"))["family"])
            # Guard le as duas grafias: o default da migration 091 grava
            # family_link_enabled (snake_case) e o Rust serializa familyLinkEnabled.
            self.assertIn("family_link_enabled", query)
            self.assertIn("familyLinkEnabled", query)
            await conn.execute("""UPDATE partner_programs SET experience_config =
                jsonb_set(experience_config, '{features,family_link_enabled}', 'true'::jsonb) WHERE id = $1""", self.prog_a)
            self.assertTrue((await conn.fetchrow(query, "family"))["family"])
            # camelCase prevalece quando as duas chaves existem.
            await conn.execute("""UPDATE partner_programs SET experience_config = jsonb_set(
                jsonb_set(experience_config, '{features,family_link_enabled}', 'false'::jsonb),
                '{features,familyLinkEnabled}', 'true'::jsonb) WHERE id = $1""", self.prog_a)
            self.assertTrue((await conn.fetchrow(query, "family"))["family"])
            await conn.execute("""UPDATE partner_programs SET experience_config =
                jsonb_set(experience_config, '{features,familyLinkEnabled}', 'false'::jsonb) WHERE id = $1""", self.prog_a)
            self.assertFalse((await conn.fetchrow(query, "family"))["family"])
            await conn.execute("""UPDATE partner_programs SET experience_config =
                jsonb_set(experience_config, '{features,familyLinkEnabled}', 'true'::jsonb) WHERE id = $1""", self.prog_a)
            self.assertTrue((await conn.fetchrow(query, "family"))["family"])
            await conn.execute("UPDATE program_enrollments SET family_share_enabled = false WHERE program_id = $1", self.prog_a)
            self.assertFalse((await conn.fetchrow(query, "family"))["family"])
            await conn.execute("UPDATE program_enrollments SET status = 'REMOVED' WHERE program_id = $1", self.prog_a)
            self.assertFalse((await conn.fetchrow(query, "patient-1"))["participant"])
            await conn.execute("UPDATE partner_programs SET closed_at = NOW() WHERE id = $1", self.prog_a)
            self.assertFalse((await conn.fetchrow(query, "owner-a"))["owner"])

    async def test_wallet_kpis_are_global_bounded_and_exclude_other_tenants_and_old_consent(self):
        import json
        sql_dir = ROOT / "rust/crates/braga-edge/src/sql"
        base = (sql_dir / "b2b_wallet.sql").read_text()
        global_query = base + (sql_dir / "b2b_wallet_global.sql").read_text()
        page_query = base + " SELECT * FROM metrics ORDER BY patient_user_id"
        now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO program_enrollments (program_id, patient_user_id, status, shared_categories)
                VALUES ($1, 'patient-2', 'ACTIVE', '[]'::jsonb)""", self.prog_a)
            await conn.execute("UPDATE program_enrollments SET consented_at = $1", now - timedelta(days=30))
            # Current + previous-week actions by the same participant. Company updates alone are not activity.
            for days in (1, 8):
                await conn.execute("""INSERT INTO program_appointments
                    (program_id, patient_user_id, modality, requested_slot_text, status, scheduled_at, created_at, updated_at)
                    VALUES ($1, 'patient-1', 'IN_PERSON', 'Manhã', 'CONFIRMED', $2, $3, $4)""",
                    self.prog_a, now + timedelta(days=2), now - timedelta(days=days), now)
            # A foreign tenant must never contribute even with the same patient ID.
            await conn.execute("""INSERT INTO program_appointments
                (program_id, patient_user_id, modality, requested_slot_text, created_at)
                VALUES ($1, 'patient-2', 'IN_PERSON', 'Tarde', $2)""", self.prog_b, now)
            reminder = await conn.fetchval("""INSERT INTO program_reminders
                (program_id, target_category, title, body, created_by, created_at, expires_at)
                VALUES ($1, 'steps', 'Rotina', 'Confira sua rotina', 'owner-a', $2, $3) RETURNING id""",
                self.prog_a, now - timedelta(days=1), now + timedelta(days=1))
            args = (self.prog_a, None, ["steps"], True, True, now)
            rows = await conn.fetch(page_query, *args)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["active_days"], 1)
            # date[] -> text[] com day anterior (UTC), 7 dias incluindo hoje.
            self.assertEqual(rows[0]["activity_dates"], ["2026-11-19"])
            self.assertEqual(rows[0]["pending_reminders"], 1)
            self.assertEqual(rows[0]["upcoming_appointments"], 2)
            self.assertEqual(rows[1]["active_days"], 0)
            self.assertEqual(rows[1]["activity_dates"], [])
            self.assertEqual(rows[1]["pending_reminders"], 0)  # no category consent
            kpis = json.loads(await conn.fetchval(global_query, *args))
            self.assertEqual((kpis["activeUsers"], kpis["inactiveUsers"]), (1, 1))
            self.assertEqual(kpis["retentionRate"], 100.0)
            self.assertEqual(kpis["appointmentsConfirmed"], 2)
            # Page IDs bound details without affecting the independent global query.
            page = await conn.fetch(page_query, self.prog_a, ["patient-2"], ["steps"], True, True, now)
            self.assertEqual([r["patient_user_id"] for r in page], ["patient-2"])
            await conn.execute("""INSERT INTO program_reminder_receipts
                (reminder_id, patient_user_id, acknowledged_at, acknowledged_by_uid)
                VALUES ($1, 'patient-1', $2, 'patient-1')""", reminder, now)
            self.assertEqual(json.loads(await conn.fetchval(global_query, *args))["remindersRead"], 1)
            # Revoked consent excludes category-targeted reminders immediately.
            await conn.execute("UPDATE program_enrollments SET shared_categories = '[]'::jsonb WHERE program_id = $1", self.prog_a)
            self.assertEqual(json.loads(await conn.fetchval(global_query, *args))["remindersRead"], 0)
            # Re-enrollment must not expose actions predating the new consent.
            await conn.execute("UPDATE program_enrollments SET consented_at = $2 WHERE program_id = $1", self.prog_a, now)
            kpis = json.loads(await conn.fetchval(global_query, *args))
            self.assertEqual(kpis["activeUsers"], 0)
            self.assertIsNone(kpis["retentionRate"])
            empty = json.loads(await conn.fetchval(global_query, self.prog_draft, None, ["steps"], True, True, now))
            self.assertEqual(empty["activeUsers"], 0)
            self.assertEqual(empty["inactiveUsers"], 0)
            self.assertIsNone(empty["retentionRate"])

    async def test_b2b_30_account_mode_defaults_and_check_constraint(self):
        async with self.pool.acquire() as conn:
            # 093 faz backfill dos owners com programa aberto para DUAL; o default
            # PERSONAL permanece para todas as outras contas.
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'patient-1'"
                ),
                "PERSONAL",
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'owner-a'"
                ),
                "DUAL",
            )
            # 093 roda de novo sem efeito extra (idempotente).
            async with conn.transaction():
                await conn.execute(MIG_093_PATH.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'owner-a'"
                ),
                "DUAL",
            )

            await conn.execute(
                "INSERT INTO profiles (id, full_name, user_role, account_mode) "
                "VALUES ('biz-1', '', NULL, 'BUSINESS_ONLY')"
            )
            biz = await conn.fetchrow(
                "SELECT account_mode, user_role, basic_profile_complete, self_care_complete "
                "FROM profiles WHERE id = 'biz-1'"
            )
            self.assertEqual(biz["account_mode"], "BUSINESS_ONLY")
            self.assertIsNone(biz["user_role"])
            self.assertFalse(biz["basic_profile_complete"])
            self.assertFalse(biz["self_care_complete"])

            with self.assertRaises(Exception):
                await conn.execute(
                    "UPDATE profiles SET account_mode = 'HACKER' WHERE id = 'biz-1'"
                )

            await conn.execute(
                "UPDATE profiles SET account_mode = 'DUAL' WHERE id = 'biz-1'"
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'biz-1'"
                ),
                "DUAL",
            )

    async def test_migration_093_backfills_only_open_program_owners(self):
        sql_up = MIG_093_PATH.read_text(encoding="utf-8-sig")
        sql_down = MIG_093_ROLLBACK_PATH.read_text(encoding="utf-8-sig")
        async with self.pool.acquire() as conn:
            # Owner de programa ENCERRADO nao e backfilled.
            await conn.execute(
                "INSERT INTO profiles (id, full_name) VALUES ('owner-closed', 'Responsavel Encerrado')"
            )
            await conn.execute(
                """
                INSERT INTO partner_programs
                    (owner_user_id, program_name, company_name, approval_status, closed_at)
                VALUES ('owner-closed', 'Programa Encerrado', 'Clinica Encerrada', 'APPROVED', NOW())
                """
            )
            async with conn.transaction():
                await conn.execute(sql_up)
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'owner-closed'"
                ),
                "PERSONAL",
            )
            # Reaplicar e idempotente e nao rebaixa um DUAL escolhido pelo usuario.
            await conn.execute(
                "UPDATE profiles SET account_mode = 'DUAL' WHERE id = 'patient-1'"
            )
            async with conn.transaction():
                await conn.execute(sql_up)
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'patient-1'"
                ),
                "DUAL",
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'owner-a'"
                ),
                "DUAL",
            )
            # Rollback best-effort: owners com programa aberto voltam a PERSONAL.
            async with conn.transaction():
                await conn.execute(sql_down)
            self.assertEqual(
                await conn.fetchval(
                    "SELECT account_mode FROM profiles WHERE id = 'owner-a'"
                ),
                "PERSONAL",
            )

    async def test_multi_tenant_isolation_and_revocation(self):
        async with self.pool.acquire() as conn:
            rows_a = await conn.fetch(
                """
                SELECT e.patient_user_id, e.family_share_enabled, p.template_key
                  FROM program_enrollments e
                  JOIN partner_programs p ON p.id = e.program_id
                 WHERE p.owner_user_id = 'owner-a' AND e.status = 'ACTIVE'
                """
            )
            self.assertEqual([r["patient_user_id"] for r in rows_a], ["patient-1"])
            self.assertTrue(rows_a[0]["family_share_enabled"])
            self.assertEqual(rows_a[0]["template_key"], "CLINICA_GERAL")

            cross_check = await conn.fetchrow(
                """
                SELECT e.status, e.shared_categories
                  FROM partner_programs p
                  LEFT JOIN program_enrollments e
                    ON e.program_id = p.id AND e.patient_user_id = 'patient-2'
                 WHERE p.owner_user_id = 'owner-a'
                """
            )
            self.assertIsNone(cross_check["status"])

            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE partner_programs SET template_key = 'INVALID_TEMPLATE' WHERE id = $1",
                    self.prog_a,
                )

            await conn.execute(
                """
                UPDATE program_enrollments
                   SET status = 'REMOVED', shared_categories = '[]'::jsonb,
                       family_share_enabled = false, revoked_at = NOW()
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )
            active_count_a = await conn.fetchval(
                "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1 AND status = 'ACTIVE'",
                self.prog_a,
            )
            self.assertEqual(active_count_a, 0)
            removed_row = await conn.fetchrow(
                "SELECT status, shared_categories, family_share_enabled "
                "FROM program_enrollments WHERE program_id = $1 AND patient_user_id = 'patient-1'",
                self.prog_a,
            )
            self.assertEqual(removed_row["status"], "REMOVED")
            self.assertEqual(removed_row["shared_categories"], "[]")
            self.assertFalse(removed_row["family_share_enabled"])

    async def test_lgpd_delete_account_succeeds_for_draft_and_approved_b2b_owners(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE partner_programs
                   SET logo_url = $2,
                       proof_documents = $3::jsonb
                 WHERE id = $1
                """,
                self.prog_a,
                f"https://api.bragasaude.online/api/files/b2b-logos/{self.prog_a}/logo-a.png",
                f'[{{"id":"proof-1","storageKey":"b2b-kyb/{self.prog_a}/proof-1.pdf"}}]',
            )

        deleted_r2_keys = []

        def fake_delete_r2(keys):
            deleted_r2_keys.extend(keys)

        with patch.object(account_deletion, "_delete_r2_objects_sync", side_effect=fake_delete_r2):
            res_draft = await account_deletion.delete_account(
                self.pool, "owner-draft", actor="próprio titular"
            )
            self.assertTrue(res_draft["deleted"])

            res_approved = await account_deletion.delete_account(
                self.pool, "owner-a", actor="próprio titular"
            )
            self.assertTrue(res_approved["deleted"])

        self.assertIn(f"b2b-logos/{self.prog_a}/logo-a.png", deleted_r2_keys)
        self.assertIn(f"b2b-kyb/{self.prog_a}/proof-1.pdf", deleted_r2_keys)

        async with self.pool.acquire() as conn:
            remaining_programs = await conn.fetch(
                "SELECT owner_user_id FROM partner_programs ORDER BY owner_user_id"
            )
            self.assertEqual([r["owner_user_id"] for r in remaining_programs], ["owner-b"])

            enrollments_a = await conn.fetchval(
                "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1",
                self.prog_a,
            )
            self.assertEqual(enrollments_a, 0)

            patient_1_still_exists = await conn.fetchval(
                "SELECT COUNT(*) FROM profiles WHERE id = 'patient-1'"
            )
            self.assertEqual(patient_1_still_exists, 1)

            leaked_audit_actors = await conn.fetchval(
                "SELECT COUNT(*) FROM program_audit_logs "
                "WHERE actor_user_id IN ('owner-a', 'owner-draft') "
                "   OR target_user_id IN ('owner-a', 'owner-draft')"
            )
            self.assertEqual(leaked_audit_actors, 0)

    async def test_091b_unique_active_cnpj_cache_and_b2b_notifications(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO profiles (id, full_name) VALUES ('owner-dup', 'Outro Dono'), ('owner-ok', 'Dono Apos Rejeicao')"
            )
            with self.assertRaises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO partner_programs (owner_user_id, program_name, company_name, cnpj, approval_status)
                    VALUES ('owner-dup', 'Clone A', 'Clinica Clone', '12.345.678/0001-95', 'PENDING_REVIEW')
                    """
                )

            rejected_id = await conn.fetchval(
                """
                INSERT INTO partner_programs (owner_user_id, program_name, company_name, cnpj, approval_status)
                VALUES ('owner-dup', 'Rejeitado B', 'Clinica Rejeitada', '11.222.333/0001-81', 'REJECTED')
                RETURNING id
                """
            )
            self.assertIsNotNone(rejected_id)
            allowed_id = await conn.fetchval(
                """
                INSERT INTO partner_programs (owner_user_id, program_name, company_name, cnpj, approval_status)
                VALUES ('owner-ok', 'Valido B', 'Clinica Valida', '11.222.333/0001-81', 'APPROVED')
                RETURNING id
                """
            )
            self.assertIsNotNone(allowed_id)

            await conn.execute(
                """
                INSERT INTO cnpj_lookup_cache (cnpj_digits, provider, payload, expires_at)
                VALUES ('12345678000195', 'brasilapi', '{"cnpj":"12345678000195"}'::jsonb, NOW() + INTERVAL '24 hours')
                """
            )
            cached_provider = await conn.fetchval(
                "SELECT provider FROM cnpj_lookup_cache WHERE cnpj_digits = '12345678000195'"
            )
            self.assertEqual(cached_provider, "brasilapi")

            notif_id = await conn.fetchval(
                """
                SELECT notification_emit(
                    'owner-a',
                    'b2b:' || $1::text || ':approved',
                    'b2b_program:' || $1::text,
                    'B2B_PROGRAM_APPROVED',
                    'CARE',
                    'b2b_program',
                    $1::text,
                    NULL,
                    'auto-kyb-v1',
                    now(),
                    NULL,
                    '{"status":"APPROVED"}'::jsonb,
                    true
                )
                """,
                str(self.prog_a),
            )
            self.assertIsNotNone(notif_id)
            outbox_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_outbox WHERE item_id = $1",
                notif_id,
            )
            self.assertEqual(outbox_count, 1)

    async def test_b2b_17_composite_cursor_filters_and_biometrics_query(self):
        async with self.pool.acquire() as conn:
            tied_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            await conn.execute(
                "INSERT INTO profiles (id, full_name) VALUES ('p-tie-1', 'Ana Tie Um'), ('p-tie-2', 'Bruno Tie Dois')"
            )
            await conn.execute(
                """
                INSERT INTO program_enrollments
                    (program_id, patient_user_id, status, shared_categories, consented_at)
                VALUES
                    ($1, 'p-tie-1', 'ACTIVE', '["steps","authorized_records"]'::jsonb, $2),
                    ($1, 'p-tie-2', 'ACTIVE', '["steps"]'::jsonb, $2)
                """,
                self.prog_a,
                tied_ts,
            )
            # Valida desempate keyset (consented_at DESC, patient_user_id ASC)
            page1 = await conn.fetch(
                """
                SELECT e.patient_user_id
                  FROM program_enrollments e
                  JOIN profiles pr ON pr.id = e.patient_user_id
                 WHERE e.program_id = $1 AND e.status = 'ACTIVE'
                   AND ($2::timestamptz IS NULL OR e.consented_at < $2 OR (e.consented_at = $2 AND e.patient_user_id > $3))
                 ORDER BY e.consented_at DESC, e.patient_user_id ASC
                 LIMIT 2
                """,
                self.prog_a,
                None,
                None,
            )
            self.assertEqual([r["patient_user_id"] for r in page1], ["patient-1", "p-tie-1"])
            page2 = await conn.fetch(
                """
                SELECT e.patient_user_id
                  FROM program_enrollments e
                  JOIN profiles pr ON pr.id = e.patient_user_id
                 WHERE e.program_id = $1 AND e.status = 'ACTIVE'
                   AND ($2::timestamptz IS NULL OR e.consented_at < $2 OR (e.consented_at = $2 AND e.patient_user_id > $3))
                 ORDER BY e.consented_at DESC, e.patient_user_id ASC
                 LIMIT 2
                """,
                self.prog_a,
                tied_ts,
                "p-tie-1",
            )
            self.assertEqual([r["patient_user_id"] for r in page2], ["p-tie-2"])

            # Filtro por categoria e busca por nome
            filtered = await conn.fetch(
                """
                SELECT e.patient_user_id
                  FROM program_enrollments e
                  JOIN profiles pr ON pr.id = e.patient_user_id
                 WHERE e.program_id = $1 AND e.status = 'ACTIVE'
                   AND pr.full_name ILIKE $2 ESCAPE '!'
                   AND e.shared_categories @> $3::jsonb
                """,
                self.prog_a,
                "%Ana%",
                '["authorized_records"]',
            )
            self.assertEqual([r["patient_user_id"] for r in filtered], ["p-tie-1"])

            # Valida leitura real da tabela biometrics
            await conn.execute(
                """
                INSERT INTO biometrics (user_id, weight_kg, height_m, imc, measured_at)
                VALUES ('p-tie-1', 72.4, 1.75, 23.6, $1)
                """,
                tied_ts,
            )
            bio_row = await conn.fetchrow(
                "SELECT weight_kg, height_m, imc FROM biometrics WHERE user_id = 'p-tie-1' ORDER BY measured_at DESC LIMIT 1"
            )
            self.assertAlmostEqual(bio_row["weight_kg"], 72.4, places=1)
            self.assertAlmostEqual(bio_row["imc"], 23.6, places=1)

    async def test_b2b_18_reminders_notifications_mute_and_medications_isolated(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO medications (user_id, name, dosage_mg, schedule_time)
                VALUES ('patient-1', 'Losartana', 50.0, '08:00')
                """
            )
            rem_id = await conn.fetchval(
                """
                INSERT INTO program_reminders
                    (program_id, target_patient_id, target_category, reminder_type, title, body, created_by)
                VALUES
                    ($1, 'patient-1', 'steps', 'ACTIVITY', 'Caminhada leve', 'Registre seus passos de hoje', 'owner-a')
                RETURNING id
                """,
                self.prog_a,
            )
            self.assertIsNotNone(rem_id)

            # notification_emit aceita PARTNER_REMINDER para participante ativo nao silenciado
            ev = await conn.fetchval(
                """
                SELECT notification_emit(
                    'patient-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-1',
                    'owner-a',
                    NOW(),
                    NOW() + interval '30 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_id}:patient-1",
                f"b2b_reminder:{rem_id}",
                str(rem_id),
            )
            self.assertIsNotNone(ev)

            # Mutar o programa bloqueia novas emissoes notification_emit (retorna NULL sem gravar outbox)
            await conn.execute(
                "UPDATE program_enrollments SET muted_at = NOW() WHERE program_id = $1 AND patient_user_id = 'patient-1'",
                self.prog_a,
            )
            muted_ev = await conn.fetchval(
                """
                SELECT notification_emit(
                    'patient-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-1',
                    'owner-a',
                    NOW(),
                    NOW() + interval '30 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_id}:patient-1:muted",
                f"b2b_reminder:{rem_id}:muted",
                str(rem_id),
            )
            self.assertIsNone(muted_ev)
            outbox_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_outbox WHERE recipient_id = 'patient-1' AND kind = 'PARTNER_REMINDER'"
            )
            self.assertEqual(outbox_count, 1)

            # Recibo de leitura e preservacao de medications pessoais
            await conn.execute(
                """
                INSERT INTO program_reminder_receipts (reminder_id, patient_user_id, acknowledged_by_uid)
                VALUES ($1, 'patient-1', 'patient-1')
                """,
                rem_id,
            )
            med_count = await conn.fetchval(
                "SELECT COUNT(*) FROM medications WHERE user_id = 'patient-1'"
            )
            self.assertEqual(med_count, 1)

    async def test_b2b_21b_ugc_abuse_report_ai_killswitch_and_buffer_cleanup(self):
        async with self.pool.acquire() as conn:
            # 1. UGC abuse report persiste e protege participante sem erro de schema
            report_id = await conn.fetchval(
                """
                INSERT INTO program_abuse_reports (program_id, reporter_user_id, reason, details)
                VALUES ($1, 'patient-1', 'CONTEUDO_INADEQUADO', 'Solicitou exame externo sem contexto')
                RETURNING id
                """,
                self.prog_a,
            )
            self.assertIsNotNone(report_id)

            # 2. Kill switch ai_paused funciona independentemente de approval_status
            await conn.execute(
                "UPDATE partner_programs SET ai_paused = true WHERE id = $1",
                self.prog_a,
            )
            ai_paused = await conn.fetchval(
                "SELECT ai_paused FROM partner_programs WHERE id = $1",
                self.prog_a,
            )
            self.assertTrue(ai_paused)

            # 3. Limpeza de buffers RDS apaga apenas expirados e preserva registros ativos
            await conn.execute(
                """
                INSERT INTO program_reminders
                    (program_id, title, body, created_by, expires_at)
                VALUES
                    ($1, 'Expirado', 'Antigo', 'owner-a', NOW() - interval '2 days'),
                    ($1, 'Vigente', 'Ativo', 'owner-a', NOW() + interval '10 days')
                """,
                self.prog_a,
            )
            await conn.execute(
                """
                DELETE FROM program_reminders
                 WHERE id IN (SELECT id FROM program_reminders WHERE expires_at < NOW() LIMIT 500)
                """
            )
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM program_reminders WHERE program_id = $1 AND title = 'Expirado'",
                self.prog_a,
            )
            kept = await conn.fetchval(
                "SELECT COUNT(*) FROM program_reminders WHERE program_id = $1 AND title = 'Vigente'",
                self.prog_a,
            )
            self.assertEqual(remaining, 0)
            self.assertEqual(kept, 1)

    async def test_b2b_19_appointments_conflict_outbox_and_consultations_isolation(self):
        async with self.pool.acquire() as conn:
            future_slot = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
            # Consulta pessoal preexistente em consultations nao pode ser alterada pela agenda B2B
            await conn.execute(
                """
                INSERT INTO consultations (user_id, title, scheduled_date, status)
                VALUES ('patient-1', 'Consulta pessoal', $1, 'CONFIRMED')
                """,
                future_slot,
            )

            appt_1 = await conn.fetchval(
                """
                INSERT INTO program_appointments
                    (program_id, patient_user_id, modality, requested_slot_text, scheduled_at, status)
                VALUES ($1, 'patient-1', 'REMOTE', 'Quinta 14h', $2, 'CONFIRMED')
                RETURNING id
                """,
                self.prog_a,
                future_slot,
            )
            appt_2 = await conn.fetchval(
                """
                INSERT INTO program_appointments
                    (program_id, patient_user_id, modality, requested_slot_text, status)
                VALUES ($1, 'patient-1', 'IN_PERSON', 'Quinta 14h15', 'PENDING')
                RETURNING id
                """,
                self.prog_a,
            )
            self.assertIsNotNone(appt_1)
            self.assertIsNotNone(appt_2)

            # Janela de conflito 30 min detecta colisao para +15min e libera para +45min
            overlap_15m = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM program_appointments
                     WHERE program_id = $1
                       AND id <> $2
                       AND status = 'CONFIRMED'
                       AND scheduled_at IS NOT NULL
                       AND ABS(EXTRACT(EPOCH FROM (scheduled_at - $3))) < 1800.0
                )
                """,
                self.prog_a,
                appt_2,
                future_slot + timedelta(minutes=15),
            )
            overlap_45m = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM program_appointments
                     WHERE program_id = $1
                       AND id <> $2
                       AND status = 'CONFIRMED'
                       AND scheduled_at IS NOT NULL
                       AND ABS(EXTRACT(EPOCH FROM (scheduled_at - $3))) < 1800.0
                )
                """,
                self.prog_a,
                appt_2,
                future_slot + timedelta(minutes=45),
            )
            self.assertTrue(overlap_15m)
            self.assertFalse(overlap_45m)

            # Notificacao Push FCM PARTNER_APPOINTMENT (082 AGENDA) funciona para dono e participante
            item_owner = await conn.fetchval(
                """
                SELECT notification_emit(
                    'owner-a', $1, $2, 'PARTNER_APPOINTMENT', 'AGENDA',
                    'b2b_appointment', $3, 'patient-1', 'patient-1',
                    NOW(), NOW() + interval '30 days', '{"status":"PENDING"}'::jsonb, true
                )
                """,
                f"b2b_appt:{appt_2}:PENDING:owner-a",
                f"b2b_appt:{appt_2}",
                str(appt_2),
            )
            item_patient = await conn.fetchval(
                """
                SELECT notification_emit(
                    'patient-1', $1, $2, 'PARTNER_APPOINTMENT', 'AGENDA',
                    'b2b_appointment', $3, 'patient-1', 'owner-a',
                    NOW(), NOW() + interval '30 days', '{"status":"CONFIRMED"}'::jsonb, true
                )
                """,
                f"b2b_appt:{appt_1}:CONFIRMED:patient-1",
                f"b2b_appt:{appt_1}",
                str(appt_1),
            )
            self.assertIsNotNone(item_owner)
            self.assertIsNotNone(item_patient)

            # Tabela pessoal consultations permanece intacta (1 registro pessoal)
            personal_count = await conn.fetchval(
                "SELECT COUNT(*) FROM consultations WHERE user_id = 'patient-1'"
            )
            self.assertEqual(personal_count, 1)

    async def test_b2b_20_direct_chat_isolation_read_at_and_no_family_leak(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO program_direct_threads (program_id, patient_user_id, handling_mode)
                VALUES ($1, 'patient-1', 'AI_ACTIVE')
                """,
                self.prog_a,
            )
            msg_patient = await conn.fetchval(
                """
                INSERT INTO program_direct_messages
                    (program_id, patient_user_id, sender_user_id, sender_role, body)
                VALUES ($1, 'patient-1', 'patient-1', 'PATIENT', 'Posso remarcar para sexta?')
                RETURNING id
                """,
                self.prog_a,
            )
            msg_partner = await conn.fetchval(
                """
                INSERT INTO program_direct_messages
                    (program_id, patient_user_id, sender_user_id, sender_role, body)
                VALUES ($1, 'patient-1', 'owner-a', 'PARTNER', 'Claro, confirmamos sexta às 14h.')
                RETURNING id
                """,
                self.prog_a,
            )
            self.assertIsNotNone(msg_patient)
            self.assertIsNotNone(msg_partner)

            unread_before = await conn.fetchval(
                """
                SELECT COUNT(*) FROM program_direct_messages
                 WHERE program_id = $1
                   AND patient_user_id = 'patient-1'
                   AND sender_role = 'PATIENT'
                   AND read_at IS NULL
                """,
                self.prog_a,
            )
            self.assertEqual(unread_before, 1)

            await conn.execute(
                """
                UPDATE program_direct_messages
                   SET read_at = NOW()
                 WHERE program_id = $1
                   AND patient_user_id = 'patient-1'
                   AND sender_role = 'PATIENT'
                   AND read_at IS NULL
                """,
                self.prog_a,
            )
            unread_after = await conn.fetchval(
                """
                SELECT COUNT(*) FROM program_direct_messages
                 WHERE program_id = $1
                   AND patient_user_id = 'patient-1'
                   AND sender_role = 'PATIENT'
                   AND read_at IS NULL
                """,
                self.prog_a,
            )
            self.assertEqual(unread_after, 0)

            # Chat direto B2B nunca escreve em family_messages
            family_msg_count = await conn.fetchval("SELECT COUNT(*) FROM family_messages")
            self.assertEqual(family_msg_count, 0)

            # Push FCM PARTNER_DIRECT_MESSAGE funciona para dono e participante (sem PHI)
            notif_owner = await conn.fetchval(
                """
                SELECT notification_emit(
                    'owner-a', $1, $2, 'PARTNER_DIRECT_MESSAGE', 'CARE',
                    'b2b_direct_thread', $3, 'patient-1', 'patient-1',
                    NOW(), NOW() + interval '14 days', '{}'::jsonb, true
                )
                """,
                f"b2b_dm:{msg_patient}:owner-a",
                f"b2b_dm_thread:{self.prog_a}:patient-1",
                str(self.prog_a),
            )
            self.assertIsNotNone(notif_owner)

    async def test_b2b_20b_ai_handoff_for_update_quota_and_waiting_human(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO program_direct_threads (program_id, patient_user_id, handling_mode)
                VALUES ($1, 'patient-1', 'AI_ACTIVE')
                """,
                self.prog_a,
            )

            # 1. Humano assume conversa (Assumir atendimento -> HUMAN_ACTIVE)
            await conn.execute(
                """
                UPDATE program_direct_threads
                   SET handling_mode = 'HUMAN_ACTIVE',
                       taken_over_by = 'owner-a',
                       taken_over_at = NOW(),
                       handoff_note = 'Assumido pelo especialista',
                       updated_at = NOW()
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )

            # Lock FOR UPDATE antes de inserir resposta IA deve descartar resposta se modo != AI_ACTIVE
            async with conn.transaction():
                locked_mode = await conn.fetchval(
                    """
                    SELECT handling_mode FROM program_direct_threads
                     WHERE program_id = $1 AND patient_user_id = 'patient-1'
                     FOR UPDATE
                    """,
                    self.prog_a,
                )
                self.assertEqual(locked_mode, "HUMAN_ACTIVE")

            # 2. Devolver para Recepcionista IA -> AI_ACTIVE volta a responder e incrementa turns
            await conn.execute(
                """
                UPDATE program_direct_threads
                   SET handling_mode = 'AI_ACTIVE',
                       taken_over_by = 'owner-a',
                       taken_over_at = NOW(),
                       handoff_note = 'Devolvido para IA',
                       updated_at = NOW()
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )
            async with conn.transaction():
                locked_mode_2 = await conn.fetchval(
                    """
                    SELECT handling_mode FROM program_direct_threads
                     WHERE program_id = $1 AND patient_user_id = 'patient-1'
                     FOR UPDATE
                    """,
                    self.prog_a,
                )
                self.assertEqual(locked_mode_2, "AI_ACTIVE")
                await conn.execute(
                    """
                    UPDATE program_direct_threads
                       SET ai_turns_today = ai_turns_today + 1
                     WHERE program_id = $1 AND patient_user_id = 'patient-1'
                    """,
                    self.prog_a,
                )
                await conn.execute(
                    """
                    INSERT INTO program_direct_messages
                        (program_id, patient_user_id, sender_user_id, sender_role, body)
                    VALUES ($1, 'patient-1', 'owner-a', 'AI_RECEPTIONIST', 'Horario seg a sex 08h-18h.')
                    """,
                    self.prog_a,
                )

            monthly_ai = await conn.fetchval(
                """
                SELECT COUNT(*) FROM program_direct_messages
                 WHERE program_id = $1
                   AND sender_role = 'AI_RECEPTIONIST'
                   AND created_at >= date_trunc('month', NOW())
                """,
                self.prog_a,
            )
            self.assertEqual(monthly_ai, 1)

            # 3. Pedido humano coloca thread em WAITING_HUMAN sem tocar em vital_signs/medications
            await conn.execute(
                """
                UPDATE program_direct_threads
                   SET handling_mode = 'WAITING_HUMAN', updated_at = NOW()
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )
            mode_after_req = await conn.fetchval(
                """
                SELECT handling_mode FROM program_direct_threads
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )
            self.assertEqual(mode_after_req, "WAITING_HUMAN")

    async def test_b2b_21_family_link_double_key_and_caregiver_ack(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO profiles (id, full_name) VALUES
                    ('caregiver-1', 'Cuidadora Maria'),
                    ('stranger-1', 'Estranho Sem Vinculo')
                ON CONFLICT DO NOTHING
                """
            )
            await conn.execute(
                """
                INSERT INTO family_bindings
                    (patient_id, caregiver_id, caregiver_name, caregiver_relation, connection_code, status, expires_at)
                VALUES
                    ('patient-1', 'caregiver-1', 'Cuidadora Maria', 'Filha', 'FAMB2101', 'ACTIVE', NOW() + interval '7 days'),
                    ('patient-2', 'caregiver-1', 'Cuidadora Maria', 'Filha', 'FAMB2102', 'ACTIVE', NOW() + interval '7 days')
                """
            )

            # 1. Trava dupla: prog_a tem family_link_enabled=false por padrao enquanto patient-1 tem family_share_enabled=true.
            rem_id = await conn.fetchval(
                """
                INSERT INTO program_reminders
                    (program_id, target_patient_id, target_category, reminder_type, title, body, created_by)
                VALUES
                    ($1, 'patient-1', 'steps', 'ROUTINE', 'Caminhada leve', 'Registre seus passos de hoje', 'owner-a')
                RETURNING id
                """,
                self.prog_a,
            )

            suppressed = await conn.fetchval(
                """
                SELECT notification_emit(
                    'caregiver-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-1',
                    'owner-a',
                    NOW(),
                    NOW() + interval '7 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_id}:caregiver-1:off",
                f"b2b_reminder:{rem_id}",
                str(rem_id),
            )
            self.assertIsNone(suppressed)

            # 2. Habilita chave do programa (family_link_enabled = true): dupla-chave fecha para patient-1 (family_share=true)
            #    mas continua fechada para patient-2 (family_share=false).
            await conn.execute(
                """
                UPDATE partner_programs
                   SET experience_config = jsonb_set(experience_config, '{features,family_link_enabled}', 'true'::jsonb)
                 WHERE id = $1
                """,
                self.prog_a,
            )
            await conn.execute(
                """
                UPDATE partner_programs
                   SET experience_config = jsonb_set(experience_config, '{features,family_link_enabled}', 'true'::jsonb)
                 WHERE id = $1
                """,
                self.prog_b,
            )

            delivered = await conn.fetchval(
                """
                SELECT notification_emit(
                    'caregiver-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-1',
                    'owner-a',
                    NOW(),
                    NOW() + interval '7 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_id}:caregiver-1:on",
                f"b2b_reminder:{rem_id}",
                str(rem_id),
            )
            self.assertIsNotNone(delivered)

            rem_b = await conn.fetchval(
                """
                INSERT INTO program_reminders
                    (program_id, target_patient_id, reminder_type, title, body, created_by)
                VALUES
                    ($1, 'patient-2', 'ROUTINE', 'Aviso B', 'Checklist diario', 'owner-b')
                RETURNING id
                """,
                self.prog_b,
            )
            blocked_patient2 = await conn.fetchval(
                """
                SELECT notification_emit(
                    'caregiver-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-2',
                    'owner-b',
                    NOW(),
                    NOW() + interval '7 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_b}:caregiver-1",
                f"b2b_reminder:{rem_b}",
                str(rem_b),
            )
            self.assertIsNone(blocked_patient2)

            blocked_stranger = await conn.fetchval(
                """
                SELECT notification_emit(
                    'stranger-1',
                    $1,
                    $2,
                    'PARTNER_REMINDER',
                    'CARE',
                    'b2b_reminder',
                    $3,
                    'patient-1',
                    'owner-a',
                    NOW(),
                    NOW() + interval '7 days',
                    '{}'::jsonb,
                    true
                )
                """,
                f"b2b_reminder:{rem_id}:stranger-1",
                f"b2b_reminder:{rem_id}",
                str(rem_id),
            )
            self.assertIsNone(blocked_stranger)

            # 3. Cuidador confirma leitura gravando acknowledged_by_uid = 'caregiver-1'
            ack_by = await conn.fetchval(
                """
                INSERT INTO program_reminder_receipts (reminder_id, patient_user_id, acknowledged_by_uid)
                VALUES ($1, 'patient-1', 'caregiver-1')
                ON CONFLICT (reminder_id, patient_user_id) DO UPDATE
                   SET acknowledged_by_uid = COALESCE(program_reminder_receipts.acknowledged_by_uid, EXCLUDED.acknowledged_by_uid)
                RETURNING acknowledged_by_uid
                """,
                rem_id,
            )
            self.assertEqual(ack_by, "caregiver-1")

            # 4. Visao do overview por SQL reflete dupla-chave e exclui patient-2
            visible_progs = await conn.fetch(
                """
                SELECT e.program_id, e.patient_user_id
                  FROM program_enrollments e
                  JOIN partner_programs pr ON pr.id = e.program_id
                  JOIN family_bindings fb
                    ON fb.patient_id = e.patient_user_id
                   AND fb.caregiver_id = 'caregiver-1'
                   AND fb.status = 'ACTIVE'
                 WHERE e.status = 'ACTIVE'
                   AND COALESCE(e.family_share_enabled, false) = true
                   AND COALESCE((pr.experience_config->'features'->>'family_link_enabled')::boolean, false) = true
                """
            )
            self.assertEqual([(r["program_id"], r["patient_user_id"]) for r in visible_progs], [(self.prog_a, "patient-1")])

    async def test_chat_01_production_relay_queries_keep_short_buffers_and_tenant_scope(self):
        """Executa o mesmo SQL incluido no Rust; retencao local nao amplia o RDS."""
        try:
            from purge_retention_data import purge_family_messages
        except ImportError:
            from scripts.server.purge_retention_data import purge_family_messages

        sql_dir = ROOT / "rust/crates/braga-edge/sql"
        page_sql = (sql_dir / "direct_messages_page.sql").read_text(encoding="utf-8")
        purge_sql = (sql_dir / "purge_direct_messages.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO program_direct_messages
                   (program_id, patient_user_id, sender_user_id, sender_role, body, created_at)
                   SELECT $1, 'patient-1', 'owner-a', 'PARTNER', 'buffer expirado',
                          NOW() - interval '8 days' FROM generate_series(1, 501)""",
                self.prog_a,
            )
            saved = await conn.fetchval(
                """INSERT INTO program_direct_messages
                   (program_id, patient_user_id, sender_user_id, sender_role, body, created_at)
                   VALUES ($1, 'patient-1', 'owner-a', 'PARTNER', '48h no relay', NOW() - interval '48 hours')
                   RETURNING id""",
                self.prog_a,
            )
            await conn.execute(
                """INSERT INTO program_direct_messages
                   (program_id, patient_user_id, sender_user_id, sender_role, body)
                   VALUES ($1, 'patient-2', 'owner-b', 'PARTNER', 'Outra clinica')""",
                self.prog_b,
            )
            page = await conn.fetch(page_sql, self.prog_a, "patient-1", None, None, 51)
            self.assertEqual([r["message_id"] for r in page], [str(saved)])
            self.assertEqual(
                await conn.fetch(page_sql, self.prog_b, "patient-1", None, None, 51),
                [],
            )
            self.assertEqual(await conn.execute(purge_sql), "DELETE 500")
            self.assertEqual(await conn.execute(purge_sql), "DELETE 1")
            self.assertEqual(await conn.execute(purge_sql), "DELETE 0")
            self.assertEqual(
                await conn.fetchval("SELECT COUNT(*) FROM program_direct_messages"), 2
            )

            await conn.execute(
                """INSERT INTO family_messages
                   (patient_id, sender_id, sender_name, message_text, sent_at, expires_at)
                   VALUES ('patient-1', 'patient-1', 'Sintetico', 'expirado',
                           NOW() - interval '48 hours', NOW() - interval '24 hours'),
                          ('patient-1', 'patient-1', 'Sintetico', 'entrega pendente',
                           NOW(), NOW() + interval '24 hours')"""
            )
            self.assertEqual(await purge_family_messages(conn), 1)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM family_messages"), 1)
            self.assertEqual(
                await conn.fetchval("SELECT COUNT(*) FROM program_direct_messages"), 2
            )

    async def test_b2b_22_lgpd_cascade_delete_and_multi_tenant_nocode_isolation(self):
        async with self.pool.acquire() as conn:
            # Popula todas as tabelas B2B para patient-1 (prog_a) e patient-2 (prog_b)
            rem_a = await conn.fetchval(
                """
                INSERT INTO program_reminders
                    (program_id, target_patient_id, target_category, reminder_type, title, body, created_by)
                VALUES
                    ($1, 'patient-1', 'steps', 'ROUTINE', 'Lembrete A', 'Meta passos', 'owner-a')
                RETURNING id
                """,
                self.prog_a,
            )
            await conn.execute(
                """
                INSERT INTO program_reminder_receipts (reminder_id, patient_user_id, acknowledged_by_uid)
                VALUES ($1, 'patient-1', 'patient-1')
                """,
                rem_a,
            )
            await conn.execute(
                """
                INSERT INTO program_appointments
                    (program_id, patient_user_id, modality, requested_slot_text, status)
                VALUES
                    ($1, 'patient-1', 'REMOTE', 'Quinta 10h', 'PENDING'),
                    ($2, 'patient-2', 'IN_PERSON', 'Sexta 11h', 'CONFIRMED')
                """,
                self.prog_a,
                self.prog_b,
            )
            await conn.execute(
                """
                INSERT INTO program_direct_threads (program_id, patient_user_id, handling_mode)
                VALUES ($1, 'patient-1', 'AI_ACTIVE'), ($2, 'patient-2', 'HUMAN_ACTIVE')
                """,
                self.prog_a,
                self.prog_b,
            )
            await conn.execute(
                """
                INSERT INTO program_direct_messages
                    (program_id, patient_user_id, sender_user_id, sender_role, body)
                VALUES
                    ($1, 'patient-1', 'patient-1', 'PATIENT', 'Msg paciente A'),
                    ($1, 'patient-1', 'owner-a', 'PARTNER', 'Resposta clinica A'),
                    ($2, 'patient-2', 'patient-2', 'PATIENT', 'Msg paciente B')
                """,
                self.prog_a,
                self.prog_b,
            )
            await conn.execute(
                """
                INSERT INTO program_abuse_reports (program_id, reporter_user_id, reason)
                VALUES ($1, 'patient-1', 'SPAM')
                """,
                self.prog_a,
            )

            # Exclusao LGPD do titular patient-1 remove em cascata todas as linhas dele sem quebrar FKs
            await conn.execute("DELETE FROM profiles WHERE id = 'patient-1'")

            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_enrollments WHERE patient_user_id = 'patient-1'"
                ),
                0,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_reminder_receipts WHERE patient_user_id = 'patient-1'"
                ),
                0,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_appointments WHERE patient_user_id = 'patient-1'"
                ),
                0,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_direct_threads WHERE patient_user_id = 'patient-1'"
                ),
                0,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_direct_messages WHERE patient_user_id = 'patient-1'"
                ),
                0,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_abuse_reports WHERE reporter_user_id = 'patient-1'"
                ),
                0,
            )

            # Carteira isolada da Clinica B (prog_b / patient-2) permanece 100% preservada
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1 AND patient_user_id = 'patient-2'",
                    self.prog_b,
                ),
                1,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_appointments WHERE program_id = $1 AND patient_user_id = 'patient-2'",
                    self.prog_b,
                ),
                1,
            )
            self.assertEqual(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM program_direct_messages WHERE program_id = $1 AND patient_user_id = 'patient-2'",
                    self.prog_b,
                ),
                1,
            )

    async def test_v196_backward_compatibility_and_clean_rollback(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_metrics (user_id, date, steps, distance_meters, calories_burned, active_minutes)
                VALUES ('patient-1', CURRENT_DATE, 4500, 3200.0, 180.0, 40)
                """
            )
            vital_id = await conn.fetchval(
                """
                INSERT INTO vital_signs (user_id, systolic_pressure, diastolic_pressure, measured_at)
                VALUES ('patient-1', 120, 80, $1)
                RETURNING id
                """,
                datetime.now(timezone.utc),
            )
            self.assertIsNotNone(vital_id)

            indexes = {
                r["indexname"]
                for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = $1",
                    self.schema,
                )
            }
            for expected_idx in (
                "idx_partner_programs_status_created",
                "idx_program_enrollments_active_program",
                "idx_program_enrollments_patient",
                "idx_program_enrollments_program_consented_id",
                "idx_program_audit_logs_program_created",
            ):
                self.assertIn(expected_idx, indexes)

            async with conn.transaction():
                await conn.execute(
                    MIG_092B_ROLLBACK_PATH.read_text(encoding="utf-8-sig")
                )
                await conn.execute(
                    MIG_092_ROLLBACK_PATH.read_text(encoding="utf-8-sig")
                )
                await conn.execute(
                    MIG_091B_ROLLBACK_PATH.read_text(encoding="utf-8-sig")
                )
                await conn.execute(
                    MIG_091_ROLLBACK_PATH.read_text(encoding="utf-8-sig")
                )
                await conn.execute(
                    (ROOT / "migrations/090_b2b_partner_programs_rollback.sql").read_text(
                        encoding="utf-8-sig"
                    )
                )
            steps_preserved = await conn.fetchval(
                "SELECT steps FROM daily_metrics WHERE user_id = 'patient-1'"
            )
            self.assertEqual(steps_preserved, 4500)


if __name__ == "__main__":
    unittest.main()
