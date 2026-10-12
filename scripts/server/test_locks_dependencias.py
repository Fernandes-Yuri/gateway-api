"""DT-015: CI e deploy consomem o mesmo lock de dependências com hashes.

Antes: `pip install -r requirements.txt` com vários `>=` sem lock transitivo,
instalações avulsas no CI (`pip install httpx asyncpg ...`) e dependências de
runtime usadas mas não declaradas (python-dotenv, importado por migrate.py).
O mesmo commit podia resolver bibliotecas diferentes entre teste e produção.

Aqui se guarda o que a dívida pedia: lock com hashes, runtime separado de
dev, e todos os consumidores a usarem o lock.
"""
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

LINHA_REQ = re.compile(r"^([A-Za-z0-9_.\[\]-]+)==([A-Za-z0-9_.-]+)")


def _pacotes(lock: Path) -> dict[str, str]:
    """Nome normalizado -> versão, de um lock."""
    pacotes: dict[str, str] = {}
    for linha in lock.read_text(encoding="utf-8").splitlines():
        m = LINHA_REQ.match(linha)
        if m:
            nome = re.sub(r"[-_.]+", "-", m.group(1)).lower()
            pacotes[nome] = m.group(2)
    return pacotes


class LockDeDependencias(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = RAIZ / "requirements.lock"
        cls.dev = RAIZ / "requirements-dev.lock"

    def test_locks_existem_e_estao_completos(self):
        """Cada lock existe e toda a linha de pacote traz versão exata + hash."""
        for lock in (self.runtime, self.dev):
            with self.subTest(lock=lock.name):
                self.assertTrue(lock.exists(), f"{lock.name} ausente")
                linhas = lock.read_text(encoding="utf-8").splitlines()
                reqs = [l for l in linhas if LINHA_REQ.match(l)]
                self.assertTrue(reqs, f"{lock.name} não declara nenhum pacote")
                bloqueadas: list[str] = []
                for i, linha in enumerate(linhas):
                    if not LINHA_REQ.match(linha):
                        continue
                    # a linha do pacote ou as seguintes trazem os hashes
                    janela = " ".join(linhas[i:i + 4])
                    if "--hash=sha256:" not in janela:
                        bloqueadas.append(linha.strip())
                self.assertEqual(
                    bloqueadas, [],
                    f"pacotes sem hash em {lock.name}: {bloqueadas}")

    def test_dev_contem_todo_o_runtime(self):
        """O lock de CI (dev) não pode omitir nenhuma dependência de runtime."""
        faltam = sorted(set(_pacotes(self.runtime)) - set(_pacotes(self.dev)))
        self.assertEqual(
            faltam, [], f"requirements-dev.lock não contém: {faltam}")

    def test_runtime_nao_contem_ferramentas_de_dev(self):
        """ruff e pytest são de CI/locais — produção não os instala."""
        pacotes = _pacotes(self.runtime)
        for ferramenta in ("ruff", "pytest", "pytest-asyncio"):
            self.assertNotIn(
                ferramenta, pacotes,
                f"{ferramenta} está no lock de runtime: produção não precisa "
                "disso; mova para requirements-dev.txt e regenere os locks.")

    def test_runtime_declara_o_que_o_codigo_importa(self):
        """Regressão da dívida: migrate.py importa dotenv; tinha de estar
        declarado no runtime (antes não estava em nenhum)."""
        self.assertIn("python-dotenv", _pacotes(self.runtime))

    def test_workflows_instalam_pelo_lock(self):
        """Nenhum workflow instala pacotes avulsos: sempre --require-hashes
        e um requirements*.lock."""
        offenders = []
        for wf in sorted((RAIZ / ".github" / "workflows").glob("*.yml")):
            conteudo = wf.read_text(encoding="utf-8")
            for linha in conteudo.splitlines():
                if "pip install" not in linha:
                    continue
                # comentário/documentação não é passo de instalação
                if linha.strip().startswith("#"):
                    continue
                if "--upgrade pip" in linha or "--upgrade" in linha:
                    continue
                if "--require-hashes" not in linha:
                    offenders.append(f"{wf.name}: sem --require-hashes -> {linha.strip()}")
                if not re.search(r"requirements(-dev)?\.lock", linha):
                    offenders.append(f"{wf.name}: não usa lock -> {linha.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_deploy_consome_o_lock_com_hashes(self):
        """deploy.sh é o caminho que toca no RDS: tem de instalar o lock."""
        deploy = (RAIZ / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("pip install --require-hashes -r requirements.lock", deploy)


if __name__ == "__main__":
    unittest.main()
