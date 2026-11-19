"""Testes B2B-13: Isolamento multi-tenant, anti-IDOR, retrocompatibilidade v1.9.6 e RDS Micro."""
import os
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import asyncpg
except ImportError:
    asyncpg = None

ROOT = Path(__file__).resolve().parents[2]
TEST_DSN = os.getenv("TEST_POSTGRES_URL")
B2B_RUST_PATH = ROOT / "rust/crates/braga-edge/src/b2b.rs"


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

        forbidden_tables = re.compile(
            r"\bFROM\s+(exams|exam_items|exam_cloud_consents|symptoms_diary)\b",
            re.IGNORECASE,
        )
        self.assertIsNone(
            forbidden_tables.search(src),
            "O motor B2B em Rust nunca deve consultar exams ou symptoms_diary.",
        )


@unittest.skipUnless(TEST_DSN and asyncpg is not None, "PostgreSQL efemero exclusivo do CI")
class B2BIsolationPostgresTest(unittest.IsolatedAsyncioTestCase):
    """Prova isolamento estrito multi-tenant e retrocompatibilidade v1.9.6 no Postgres 16."""

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
                for mig in (
                    "003_medication_schedules.sql",
                    "054_vital_signs_oxygen.sql",
                    "080_clinical_sync_integrity.sql",
                    "081_vital_signs_readings_contract.sql",
                ):
                    async with conn.transaction():
                        await conn.execute(
                            (ROOT / "migrations" / mig).read_text(encoding="utf-8-sig")
                        )
                async with conn.transaction():
                    await conn.execute(
                        (ROOT / "migrations/090_b2b_partner_programs.sql").read_text(
                            encoding="utf-8-sig"
                        )
                    )

                await conn.execute(
                    """
                    INSERT INTO profiles (id, full_name) VALUES
                        ('owner-a', 'Responsavel Clinica A'),
                        ('owner-b', 'Responsavel Clinica B'),
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
                        (owner_user_id, program_name, company_name, cnpj, approval_status, invite_code, seat_limit)
                    VALUES
                        ('owner-b', 'Programa B', 'Clinica B', '98.765.432/0001-10', 'APPROVED', 'CLINB-2222', 5)
                    RETURNING id
                    """
                )
                await conn.execute(
                    """
                    INSERT INTO program_enrollments
                        (program_id, patient_user_id, status, shared_categories)
                    VALUES
                        ($1, 'patient-1', 'ACTIVE', '["steps"]'::jsonb),
                        ($2, 'patient-2', 'ACTIVE', '["authorized_records"]'::jsonb)
                    """,
                    self.prog_a,
                    self.prog_b,
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
                SELECT e.patient_user_id
                  FROM program_enrollments e
                  JOIN partner_programs p ON p.id = e.program_id
                 WHERE p.owner_user_id = 'owner-a' AND e.status = 'ACTIVE'
                """
            )
            self.assertEqual([r["patient_user_id"] for r in rows_a], ["patient-1"])

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
                "idx_program_audit_logs_program_created",
            ):
                self.assertIn(expected_idx, indexes)

            async with conn.transaction():
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
