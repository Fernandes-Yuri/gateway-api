import re
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT_DIR / "migrations"
UP_SQL = MIGRATIONS_DIR / "090_b2b_partner_programs.sql"
ROLLBACK_SQL = MIGRATIONS_DIR / "090_b2b_partner_programs_rollback.sql"


class TestB2BMigration090Contract(unittest.TestCase):
    """Garante que a migration 090 (B2B-03) e estritamente aditiva e reversivel."""

    def test_migration_and_rollback_files_exist(self):
        self.assertTrue(UP_SQL.is_file(), f"Ausente: {UP_SQL}")
        self.assertTrue(ROLLBACK_SQL.is_file(), f"Ausente: {ROLLBACK_SQL}")

    def test_migration_timeouts_and_additive_only(self):
        sql = UP_SQL.read_text(encoding="utf-8")
        self.assertIn("SET LOCAL lock_timeout = '5s';", sql)
        self.assertIn("SET LOCAL statement_timeout = '60s';", sql)

        # Nao deve alterar nem remover tabelas existentes do app publicado
        forbidden = re.compile(r"\b(ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE)\b", re.IGNORECASE)
        self.assertIsNone(
            forbidden.search(sql),
            "Migration 090 deve ser estritamente aditiva (sem ALTER/DROP/TRUNCATE).",
        )

    def test_expected_tables_and_approval_states(self):
        sql = UP_SQL.read_text(encoding="utf-8")
        for table in ("partner_programs", "program_enrollments", "program_audit_logs"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)

        for status in (
            "DRAFT",
            "PENDING_REVIEW",
            "CHANGES_REQUESTED",
            "APPROVED",
            "REJECTED",
        ):
            self.assertIn(f"'{status}'", sql)

        self.assertIn("UNIQUE (program_id, patient_user_id)", sql)
        self.assertIn("bragasaude_app", sql)

    def test_rollback_drops_only_b2b_tables_in_dependency_order(self):
        rollback = ROLLBACK_SQL.read_text(encoding="utf-8")
        lines = [line.strip() for line in rollback.splitlines() if line.strip()]
        self.assertEqual(
            lines,
            [
                "DROP TABLE IF EXISTS program_audit_logs;",
                "DROP TABLE IF EXISTS program_enrollments;",
                "DROP TABLE IF EXISTS partner_programs;",
            ],
        )


if __name__ == "__main__":
    unittest.main()
