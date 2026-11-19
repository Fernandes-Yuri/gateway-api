"""Testes B2B-13 & B2B-15: Isolamento multi-tenant, motor No-Code (091), LGPD delete_account e retrocompatibilidade v1.9.6."""
import os
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
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
MIG_091_PATH = ROOT / "migrations/091_b2b_nocode_experience_builder.sql"
MIG_091_ROLLBACK_PATH = ROOT / "migrations/091_b2b_nocode_experience_builder_rollback.sql"


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

    def test_migration_091_is_additive_reversible_and_unblocks_lgpd_deletion(self):
        sql_up = MIG_091_PATH.read_text(encoding="utf-8")
        sql_down = MIG_091_ROLLBACK_PATH.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS template_key", sql_up)
        self.assertIn("ADD COLUMN IF NOT EXISTS experience_config", sql_up)
        self.assertIn("ON DELETE CASCADE", sql_up)
        self.assertIn("trg_profiles_b2b_lgpd_cleanup", sql_up)
        self.assertIn("DROP TRIGGER IF EXISTS trg_profiles_b2b_lgpd_cleanup", sql_down)
        self.assertIn("DROP COLUMN IF EXISTS experience_config", sql_down)

        for number in (3, 10, 17, 18, 31, 32, 33, 54, 56, 71, 80, 81, 90, 91):
            matches = [
                f
                for f in (ROOT / "migrations").glob(f"{number:03d}_*.sql")
                if not f.stem.endswith("_rollback")
            ]
            self.assertEqual(len(matches), 1, f"Migration {number:03d} nao encontrada")


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
                for number in (3, 10, 17, 18, 31, 32, 33, 54, 56, 71, 80, 81, 90, 91):
                    mig_file = next(
                        f
                        for f in (ROOT / "migrations").glob(f"{number:03d}_*.sql")
                        if not f.stem.endswith("_rollback")
                    )
                    async with conn.transaction():
                        await conn.execute(mig_file.read_text(encoding="utf-8-sig"))

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
                        ($3, 'owner-draft', 'owner-draft', 'PROGRAM_CREATED', '{}'::jsonb)
                    """,
                    self.prog_a,
                    self.prog_b,
                    self.prog_draft,
                )
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
                   SET status = 'REVOKED', shared_categories = '[]'::jsonb, revoked_at = NOW()
                 WHERE program_id = $1 AND patient_user_id = 'patient-1'
                """,
                self.prog_a,
            )
            active_count_a = await conn.fetchval(
                "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1 AND status = 'ACTIVE'",
                self.prog_a,
            )
            self.assertEqual(active_count_a, 0)

    async def test_lgpd_delete_account_succeeds_for_draft_and_approved_b2b_owners(self):
        with patch.object(account_deletion, "cleanup_account_files", AsyncMock(return_value=True)):
            res_draft = await account_deletion.delete_account(
                self.pool, "owner-draft", actor="próprio titular"
            )
            self.assertTrue(res_draft["deleted"])

            res_approved = await account_deletion.delete_account(
                self.pool, "owner-a", actor="próprio titular"
            )
            self.assertTrue(res_approved["deleted"])

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
