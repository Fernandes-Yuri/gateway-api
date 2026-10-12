"""Testes do ensaio de migrations (DT-004) — só as partes offline.

A execução real do ensaio é o passo "Ensaio de migrations" do CI, que
sobe um Postgres efêmero. Aqui ficam as partes puras: diff de migrations,
extração de alvos de SQL e a trava que impede o ensaio contra um banco
que não é efêmero.
"""
import importlib.util
import pathlib
import tempfile
import unittest

_ESPEC = importlib.util.spec_from_file_location(
    "ensaio_migracoes",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ensaio_migracoes.py")
ensaio = importlib.util.module_from_spec(_ESPEC)
_ESPEC.loader.exec_module(ensaio)


class MigracoesNovasTests(unittest.TestCase):
    def _dirs(self, anteriores: dict, atuais: dict):
        raiz = pathlib.Path(tempfile.mkdtemp())
        base, atual = raiz / "baseline", raiz / "atual"
        base.mkdir(), atual.mkdir()
        for nome, conteudo in anteriores.items():
            (base / nome).write_text(conteudo, encoding="utf-8")
        for nome, conteudo in atuais.items():
            (atual / nome).write_text(conteudo, encoding="utf-8")
        return base, atual

    def test_detecta_adicionada_e_alterada_e_ignora_rollback(self):
        base, atual = self._dirs(
            {"001_base.sql": "SELECT 1;"},
            {"001_base.sql": "SELECT 1;", "002_nova.sql": "SELECT 2;",
             "002_nova_rollback.sql": "SELECT 3;"})
        novas = ensaio.migracoes_novas(base, atual)
        self.assertEqual(sorted(novas), ["002_nova.sql"])

    def test_alteracao_de_conteudo_conta_como_nova(self):
        base, atual = self._dirs(
            {"001_base.sql": "SELECT 1;"},
            {"001_base.sql": "SELECT 2;"})
        self.assertEqual(list(ensaio.migracoes_novas(base, atual)), ["001_base.sql"])

    def test_sem_diferenca_devolve_vazio(self):
        base, atual = self._dirs({"001_base.sql": "SELECT 1;"},
                                 {"001_base.sql": "SELECT 1;"})
        self.assertEqual(ensaio.migracoes_novas(base, atual), {})


class AlvosDeSqlTests(unittest.TestCase):
    def test_extrai_tabelas_de_alter_update_e_insert(self):
        sql = (
            "ALTER TABLE perfis ADD COLUMN x TEXT;\n"
            "UPDATE ONLY public.registros SET y = 1;\n"
            "INSERT INTO saude (a) VALUES (1);\n"
            "DELETE FROM listas WHERE 1=0;\n"
        )
        self.assertEqual(sorted(set(ensaio.ALVO.findall(sql))),
                         ["listas", "perfis", "registros", "saude"])

    def test_nao_pega_palavras_comuns(self):
        self.assertEqual(ensaio.ALVO.findall("CREATE TABLE t (id INT);"), [])


class TravaDeBancoNaoEfemeroTests(unittest.TestCase):
    def test_banco_de_producao_e_recusado(self):
        original = ensaio.DSN
        ensaio.DSN = "postgresql://user:senha@host:5432/braga_privacy"
        try:
            with self.assertRaises(SystemExit):
                ensaio._dsn_valido()
        finally:
            ensaio.DSN = original

    def test_banco_efemero_e_aceito(self):
        original = ensaio.DSN
        ensaio.DSN = "postgresql://postgres:postgres@127.0.0.1:5432/braga_migrate_ensaio"
        try:
            self.assertEqual(ensaio._dsn_valido(), ensaio.DSN)
        finally:
            ensaio.DSN = original

    def test_sem_dsn_sai_com_erro_acionavel(self):
        original = ensaio.DSN
        ensaio.DSN = None
        try:
            with self.assertRaises(SystemExit):
                ensaio._dsn_valido()
        finally:
            ensaio.DSN = original


if __name__ == "__main__":
    unittest.main()
