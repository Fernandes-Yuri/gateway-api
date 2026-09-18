"""Testes da retencao de releases de APK (D52).

A regra: manter so os K mais recentes, porem SEMPRE proteger o release
ativo e o arquivo servido. Sem pool/disco, nao apaga nada.
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from release_retention import purge_old_releases


def _fazer_dir_com_apks(nomes):
    tmp = tempfile.mkdtemp()
    for n in nomes:
        p = os.path.join(tmp, n)
        with open(p, "wb") as f:
            f.write(b"PK" + b"0" * 10)
        # mtime incremental: primeiro nome = mais antigo
        t = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(p, (t, t))
        # corrige manualmente para ordem crescente
    # re-aplica mtimes na ordem (nomes[0] mais antigo)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    for i, n in enumerate(nomes):
        os.utime(os.path.join(tmp, n), (base + i * 60, base + i * 60))
    return tmp


class _FakeConn:
    def __init__(self, ativo):
        self.ativo = ativo
        self.executed = []
    async def fetchval(self, *args, **kwargs):
        return self.ativo
    async def execute(self, *args, **kwargs):
        self.executed.append(args)
        return "DELETE 0"


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
    class _Ctx:
        def __init__(self, conn):
            self._conn = conn
        async def __aenter__(self):
            return self._conn
        async def __aexit__(self, *a):
            return False
    def acquire(self):
        return _FakePool._Ctx(self._conn)


def _mock_pool(ativo="bragasaude.apk"):
    conn = _FakeConn(ativo)
    return _FakePool(conn), conn


class RetencaoTests(unittest.TestCase):
    def test_apaga_os_mais_antigos(self):
        d = _fazer_dir_com_apks(["r1.apk", "r2.apk", "r3.apk", "r4.apk", "r5.apk"])
        try:
            pool, _ = _mock_pool()
            res = asyncio.run(purge_old_releases(pool, d, keep=3))
            # 5 arquivos, keep=3 -> apaga os 2 mais antigos
            self.assertEqual(len(res["apagados"]), 2)
            self.assertEqual(sorted(res["apagados"]), ["r1.apk", "r2.apk"])
            sobreviventes = sorted(os.listdir(d))
            self.assertEqual(sobreviventes, ["r3.apk", "r4.apk", "r5.apk"])
        finally:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)

    def test_protege_o_ativo_mesmo_sendo_antigo(self):
        # BragaSaude.apk e o mais antigo mas e o ativo -> nao pode ser apagado
        d = _fazer_dir_com_apks(["bragasaude.apk", "r2.apk", "r3.apk", "r4.apk"])
        try:
            pool, _ = _mock_pool(ativo="bragasaude.apk")
            res = asyncio.run(purge_old_releases(pool, d, keep=2))
            self.assertNotIn("bragasaude.apk", res["apagados"])
            self.assertIn("bragasaude.apk", res["mantidos"])
        finally:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)

    def test_abaixo_do_limite_nao_apaga(self):
        d = _fazer_dir_com_apks(["r1.apk", "r2.apk"])
        try:
            pool, _ = _mock_pool()
            res = asyncio.run(purge_old_releases(pool, d, keep=3))
            self.assertEqual(res["apagados"], [])
            self.assertEqual(len(os.listdir(d)), 2)
        finally:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)

    def test_sem_pool_nao_apaga_nada(self):
        d = _fazer_dir_com_apks(["r1.apk", "r2.apk", "r3.apk", "r4.apk"])
        try:
            res = asyncio.run(purge_old_releases(None, d, keep=1))
            self.assertEqual(res["apagados"], [])
        finally:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)

    def test_diretorio_inexistente_nao_quebra(self):
        pool, _ = _mock_pool()
        res = asyncio.run(purge_old_releases(pool, "/tmp/inexistente_xyz", keep=1))
        self.assertEqual(res["apagados"], [])


if __name__ == "__main__":
    unittest.main()
