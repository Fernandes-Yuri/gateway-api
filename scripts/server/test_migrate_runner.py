"""Testes do runner de migrations (DT-003).

Duas camadas:

1. Offline — o stripper de controle de transação, incluindo a regressão
   medida em migrations reais (o `continue` sem avanço de offsets fazia o
   removerem um BEGIN de bloco DO $$ ou manterem um COMMIT; de nível zero).
2. Com Postgres — atomicidade de DDL + ledger na mesma transação
   (TEST_POSTGRES_URL do CI; saltado localmente).

Rodar: python -m unittest scripts/server/test_migrate_runner.py
"""
import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path

import migrate

CTRL = ("BEGIN;", "COMMIT;", "BEGIN", "COMMIT")


def _strip_de_referencia(sql: str) -> str:
    """Implementação de referência independente (offsets por índice no texto).

    Feita de propósito por outro caminho que o código em produção: os
    inícios de linha vêm de `re.finditer(r"\\n")`, não de `splitlines()`.
    Concordar nas 55 migrations reais é a prova de que os offsets não derivam.
    """
    token = re.compile(r"\$[A-Za-z_0-9]*\$")
    spans, abertos = [], []
    for m in token.finditer(sql):
        tag = m.group(0)
        if abertos and abertos[-1][1] == tag:
            ini, _ = abertos.pop()
            spans.append((ini, m.end()))
        else:
            abertos.append((m.start(), tag))
    if abertos:
        return sql

    inicios = [0] + [m.end() for m in re.finditer(r"\n", sql)]
    linhas = []
    for i, ini in enumerate(inicios):
        prox = inicios[i + 1] if i + 1 < len(inicios) else len(sql)
        if prox == ini:
            continue  # segmento vazio após o \n final (o producao tambem nao emite)
        corpo = sql[ini:prox].rstrip("\r\n")
        ctrl = corpo.strip().upper() in CTRL
        dentro = any(s <= ini < e for s, e in spans)
        if not (ctrl and not dentro):
            linhas.append(corpo)
    return "\n".join(linhas)


class StripTransactionControlTests(unittest.TestCase):
    def test_remove_controle_de_nivel_zero(self):
        sql = "BEGIN;\nCREATE TABLE t (id INT);\nCOMMIT;\n"
        self.assertEqual(migrate._strip_transaction_control(sql),
                         "CREATE TABLE t (id INT);")

    def test_remove_com_e_sem_ponto_e_virgula(self):
        sql = "BEGIN\nSELECT 1;\nCOMMIT\n"
        self.assertEqual(migrate._strip_transaction_control(sql), "SELECT 1;")

    def test_regressao_preserva_begin_plpgsql_do_dollar(self):
        # Caso medido na auditoria (DT-003, evidência
        # reproducao-stripper-migration.json): o stripper antigo perdia o
        # BEGIN interno e mantinha o COMMIT; de nível zero.
        sql = "BEGIN;\nDO $$\nBEGIN\nPERFORM 1;\nEND;\n$$;\nCOMMIT;\n"
        self.assertEqual(migrate._strip_transaction_control(sql),
                         "DO $$\nBEGIN\nPERFORM 1;\nEND;\n$$;")

    def test_regressao_offsets_nao_deriva_apos_remocao(self):
        # Reproduz o padrão de 023_social_posts_visibility.sql: uma
        # remoção de nível zero ANTES do bloco DO $$ deslocava os offsets e
        # o BEGIN interno era tratado como controle de transação.
        sql = (
            "BEGIN;\n"
            "CREATE TABLE a (id INT);\n"
            "DO $$\n"
            "BEGIN\n"
            "    ALTER TABLE a ADD COLUMN b TEXT;\n"
            "END;\n"
            "$$;\n"
            "COMMIT;\n"
        )
        out = migrate._strip_transaction_control(sql)
        self.assertIn("DO $$\nBEGIN\n    ALTER TABLE a ADD COLUMN b TEXT;\nEND;\n$$;", out)
        self.assertNotIn("BEGIN;", out)
        self.assertNotIn("COMMIT;", out)

    def test_regressao_remove_commit_apos_bloco_dollar(self):
        # Reproduz 040_nutrition_catalog_rds.sql: COMMIT; de nível zero que
        # o stripper antigo mantinha (commit prematuro na transacao asyncpg).
        sql = "DO $$\nBEGIN\nPERFORM 1;\nEND;\n$$;\nCOMMIT;\n"
        self.assertEqual(migrate._strip_transaction_control(sql),
                         "DO $$\nBEGIN\nPERFORM 1;\nEND;\n$$;")

    def test_preserva_corpo_de_function(self):
        sql = (
            "BEGIN;\n"
            "CREATE OR REPLACE FUNCTION f() RETURNS void AS $$\n"
            "BEGIN\n"
            "    NULL;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;\n"
            "COMMIT;\n"
        )
        out = migrate._strip_transaction_control(sql)
        self.assertIn("BEGIN\n    NULL;\nEND;\n$$ LANGUAGE plpgsql;", out)
        self.assertNotIn("BEGIN;\n", out)

    def test_nao_toca_dentro_de_function_com_tag_nomeada(self):
        sql = "DO $corpo$\nBEGIN\nPERFORM 1;\nEND;\n$corpo$;\nCOMMIT;\n"
        out = migrate._strip_transaction_control(sql)
        self.assertIn("BEGIN\nPERFORM 1;\nEND;\n$corpo$;", out)
        self.assertNotIn("COMMIT;", out)

    def test_tag_aberta_retorna_sql_intacto(self):
        sql = "BEGIN;\nDO $$\nBEGIN\n"
        self.assertEqual(migrate._strip_transaction_control(sql), sql)

    def test_sem_trailing_newline_e_crlf(self):
        casos = [
            "BEGIN;\nCREATE TABLE t (id INT);\nCOMMIT;",
            "BEGIN;\r\nCREATE TABLE t (id INT);\r\nCOMMIT;\r\n",
            "",
        ]
        for sql in casos:
            with self.subTest(repr(sql[:20])):
                self.assertEqual(migrate._strip_transaction_control(sql),
                                 _strip_de_referencia(sql))


class DiferencialSobreMigrationsReaisTests(unittest.TestCase):
    def test_stripper_concorda_com_referencia_nas_migrations(self):
        arquivos = sorted((migrate.MIGRATIONS_DIR).glob("*.sql"))
        self.assertGreater(len(arquivos), 40, "diretório real de migrations ausente")
        for f in arquivos:
            with self.subTest(migration=f.name):
                sql = f.read_text(encoding="utf-8-sig")
                self.assertEqual(migrate._strip_transaction_control(sql),
                                 _strip_de_referencia(sql))

    def test_nenhum_bloco_dollar_e_perdido(self):
        for f in sorted((migrate.MIGRATIONS_DIR).glob("*.sql")):
            sql = f.read_text(encoding="utf-8-sig")
            with self.subTest(migration=f.name):
                self.assertEqual(migrate._strip_transaction_control(sql).count("$$"),
                                 sql.count("$$"))


class ArquivosDeMigrationTests(unittest.TestCase):
    def test_exclui_rollbacks_do_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "001_base.sql").write_text("SELECT 1;", encoding="utf-8")
            (raiz / "001_base_rollback.sql").write_text("SELECT 1;", encoding="utf-8")
            (raiz / "002_outra.sql").write_text("SELECT 1;", encoding="utf-8")
            original = migrate.MIGRATIONS_DIR
            migrate.MIGRATIONS_DIR = raiz
            try:
                nomes = [f.name for f in migrate._arquivos_de_migration()]
            finally:
                migrate.MIGRATIONS_DIR = original
        self.assertEqual(nomes, ["001_base.sql", "002_outra.sql"])


DSN = os.getenv("TEST_POSTGRES_URL")


@unittest.skipUnless(DSN, "PostgreSQL efêmero disponível somente no CI")
class AtomicidadeMigrateTests(unittest.TestCase):
    """DDL + ledger na mesma transação: ou tudo aplica, ou nada."""

    TABELAS = ("dt003_probe", "dt003_meio")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.migrations = Path(self._tmp.name)
        # Limpa ANTES de mexer no módulo: se _limpar() falhar, o setUp aborta
        # e o tearDown não corre — nunca deixar globals do migrate apontando
        # para uma pasta temporária (foi assim que um erro em cascata se
        # espalhou pelo suite no CI de 11/10/2026).
        self._limpar()
        self.addCleanup(setattr, migrate, "MIGRATIONS_DIR", migrate.MIGRATIONS_DIR)
        self.addCleanup(setattr, migrate, "MIGRATIONS_DATABASE_URL",
                        migrate.MIGRATIONS_DATABASE_URL)
        migrate.MIGRATIONS_DIR = self.migrations
        migrate.MIGRATIONS_DATABASE_URL = DSN

    def tearDown(self):
        self._limpar()

    def _limpar(self):
        import asyncpg

        async def _drop():
            conn = await asyncpg.connect(DSN)
            try:
                for t in self.TABELAS:
                    await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE;")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                await conn.execute(
                    "DELETE FROM schema_migrations WHERE version LIKE 'dt003%';")
            finally:
                await conn.close()

        asyncio.run(_drop())

    def _migrar(self):
        asyncio.run(migrate.run_migrate())

    def _versoes(self):
        import asyncpg

        async def _fetch():
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                return await conn.fetchval(
                    "SELECT count(*) FROM schema_migrations WHERE version LIKE 'dt003%';")
            finally:
                await conn.close()

        return asyncio.run(_fetch())

    def _existe(self, tabela: str) -> bool:
        import asyncpg

        async def _q():
            conn = await asyncpg.connect(DSN)
            try:
                return await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL;", tabela)
            finally:
                await conn.close()

        return asyncio.run(_q())

    def test_migration_valida_aplica_ddl_e_ledger(self):
        (self.migrations / "dt003_ok.sql").write_text(
            "BEGIN;\n"
            "CREATE TABLE IF NOT EXISTS dt003_probe (id INT PRIMARY KEY);\n"
            "DO $$\n"
            "BEGIN\n"
            "    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
            "                   WHERE table_name = 'dt003_probe'\n"
            "                     AND column_name = 'nota') THEN\n"
            "        ALTER TABLE dt003_probe ADD COLUMN nota TEXT;\n"
            "    END IF;\n"
            "END;\n"
            "$$;\n"
            "COMMIT;\n", encoding="utf-8")
        self._migrar()
        self.assertTrue(self._existe("dt003_probe"))
        self.assertEqual(self._versoes(), 1)

    def test_falha_no_ddl_reverte_tudo_e_nao_registra(self):
        (self.migrations / "dt003_fail.sql").write_text(
            "CREATE TABLE dt003_meio (id INT);\n"
            "SELECT 1/0;\n", encoding="utf-8")
        with self.assertRaises(Exception):
            self._migrar()
        self.assertFalse(self._existe("dt003_meio"), "DDL parcial sobreviveu à falha")
        self.assertEqual(self._versoes(), 0, "ledger registrou versão que falhou")

    def test_falha_no_ledger_reverte_o_ddl(self):
        """Injeta falha entre o SQL e o ledger — a janela da DT-003."""
        import asyncpg

        (self.migrations / "dt003_ledger.sql").write_text(
            "CREATE TABLE dt003_probe (id INT);\n", encoding="utf-8")

        real_connect = asyncpg.connect

        class _ConnComFalhaNoLedger:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, nome):
                return getattr(self._real, nome)

            async def execute(self, sql, *args, **kwargs):
                if "INSERT INTO schema_migrations" in sql and args:
                    raise RuntimeError("falha simulada gravando o ledger")
                return await self._real.execute(sql, *args, **kwargs)

        async def _connect_com_falha(dsn):
            return _ConnComFalhaNoLedger(await real_connect(dsn))

        migrate.asyncpg.connect = _connect_com_falha
        try:
            with self.assertRaises(RuntimeError):
                self._migrar()
        finally:
            migrate.asyncpg.connect = real_connect

        self.assertFalse(self._existe("dt003_probe"),
                         "DDL aplicado sobreviveu à falha do ledger")
        self.assertEqual(self._versoes(), 0)

    def test_runner_para_na_primeira_falha_e_e_idempotente(self):
        # Nomes numerados de propósito: o runner aplica por ordem alfabética,
        # e a boa tem de correr ANTES da quebrada (com "dt003_fail" <dt003_ok"
        # a boa nunca chegava a aplicar-se e o teste só passava por contaminação
        # de estado deixada por outro teste).
        (self.migrations / "dt003_1_ok.sql").write_text(
            "CREATE TABLE IF NOT EXISTS dt003_probe (id INT);\n", encoding="utf-8")
        (self.migrations / "dt003_2_fail.sql").write_text(
            "CREATE TABLE dt003_meio (id INT);\nSELECT 1/0;\n", encoding="utf-8")
        with self.assertRaises(Exception):
            self._migrar()
        self.assertTrue(self._existe("dt003_probe"))
        self.assertEqual(self._versoes(), 1)

        # Corrige a migration quebrada e reexecuta: só ela é aplicada.
        (self.migrations / "dt003_2_fail.sql").unlink()
        (self.migrations / "dt003_2_fail.sql").write_text(
            "CREATE TABLE IF NOT EXISTS dt003_meio (id INT);\n", encoding="utf-8")
        self._migrar()
        self.assertTrue(self._existe("dt003_meio"))
        self.assertEqual(self._versoes(), 2)


if __name__ == "__main__":
    unittest.main()
