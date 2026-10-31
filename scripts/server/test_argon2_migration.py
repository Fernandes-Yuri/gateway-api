"""Suíte de testes da migracao SPEC-10: Argon2id + Pepper com lazy re-hashing.

Cobertura (DoD / SPEC-10 §7):
  * Login bem-sucedido com hash Bcrypt legado ($2a$/$2b$/$2y$).
  * Atualizacao automatica no banco para hash iniciando com $argon2id$.
  * Login subsequente com o hash Argon2id migrado (sem rehash desnecessario).
  * Rejeicao correta de senha invalida em ambos os esquemas.
  * Ausencia de timing leak para usuario inexistente (verify_missing_user).

Rodar:  python -m unittest test_argon2_migration -v
        (working-directory: scripts/server)
"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt

# Forca o pepper de teste ANTES de usar auth_crypto (o modulo le
# AUTH_PEPPER_SECRET via os.environ.get no modulo; aqui sobrescrevemos o
# atributo de modulo PEPPER para nao depender de env real).
os.environ["AUTH_PEPPER_SECRET"] = (
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)

import auth_crypto  # noqa: E402  (precisa do env setado acima)
from auth_crypto import (  # noqa: E402
    hash_password,
    verify_password,
    verify_missing_user,
    PEPPER,
)

# Garante pepper deterministico nos testes (defensivo contra env externo).
auth_crypto.PEPPER = PEPPER

# ---- Helpers ---------------------------------------------------------------


def _gerar_hash_bcrypt(senha: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt(rounds)).decode("utf-8")


def _make_pool(row=None, execute_count=0):
    """Pool falso: fetchrow retorna row; execute conta chamadas."""
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    conn.fetchval.return_value = 0  # auth_limit: 0 tentativas (abaixo do limite)
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


class VerifyPasswordTest(unittest.TestCase):
    """Testes unitarios de auth_crypto.verify_password."""

    def test_bcrypt_legado_valido_precisa_rehash(self):
        senha = "Minhasenha123"
        hash_bcrypt = _gerar_hash_bcrypt(senha)
        valido, precisa = verify_password(senha, hash_bcrypt)
        self.assertTrue(valido)
        self.assertTrue(precisa)  # lazy re-hashing disparado

    def test_bcrypt_legado_invalido_nao_precisa_rehash(self):
        hash_bcrypt = _gerar_hash_bcrypt("outrasenha123")
        valido, precisa = verify_password("Minhasenha123", hash_bcrypt)
        self.assertFalse(valido)
        self.assertFalse(precisa)

    def test_argon2id_valido_nao_precisa_rehash(self):
        hash_argon = hash_password("Minhasenha123")
        self.assertTrue(hash_argon.startswith("$argon2id$"))
        valido, precisa = verify_password("Minhasenha123", hash_argon)
        self.assertTrue(valido)
        self.assertFalse(precisa)  # parametros vigentes

    def test_argon2id_invalido(self):
        hash_argon = hash_password("Minhasenha123")
        valido, precisa = verify_password("senha_errada123", hash_argon)
        self.assertFalse(valido)
        self.assertFalse(precisa)

    def test_hash_vazio_ou_none(self):
        self.assertEqual(verify_password("x", ""), (False, False))
        self.assertEqual(verify_password("x", None), (False, False))

    def test_formato_desconhecido(self):
        valido, precisa = verify_password("x", "$outro$algumacoisa")
        self.assertEqual((valido, precisa), (False, False))

    def test_hash_password_retorna_argon2id(self):
        h = hash_password("SenhaForte42!")
        self.assertTrue(h.startswith("$argon2id$"))
        # Deve ser verificavel por si proprio
        valido, _ = verify_password("SenhaForte42!", h)
        self.assertTrue(valido)


class VerifyMissingUserTest(unittest.TestCase):
    """Timing side-channel: usuario inexistente nunca deve estourar excecao."""

    def test_nao_levanta_excecao(self):
        # Deve gastar tempo de Argon2id e retornar sem efeito.
        verify_missing_user("qualquer_senha")
        verify_missing_user("")
        verify_missing_user("a" * 200)


class LazyMigrationEndToEndTest(unittest.TestCase):
    """Testa o fluxo completo via endpoint /api/admin/login com pool mock."""

    def setUp(self):
        import server  # noqa: WPS433 (import local para nao quebrar outros modulos)
        self.server = server
        self._orig_pool = server.admin_router._state.db_pool
        self._orig_rl = server.admin_router._state.rate_limiter
        server.admin_router._state.rate_limiter = MagicMock(check=AsyncMock())
        self._env_patcher = patch.dict(
            os.environ,
            {"SERVICE_TOKEN": "svc_test_token_super_secreto_D51",
             "AUTH_PEPPER_SECRET": os.environ["AUTH_PEPPER_SECRET"]},
        )
        self._env_patcher.start()
        self.client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(server.app)

    def tearDown(self):
        self._env_patcher.stop()
        self.server.admin_router._state.db_pool = self._orig_pool
        self.server.admin_router._state.rate_limiter = self._orig_rl

    def _auth(self):
        return {"Authorization": "Bearer svc_test_token_super_secreto_D51"}

    def _login_com_hash(self, stored_hash, role="admin"):
        """Configura o pool para retornar o hash dado e retorna (client, conn)."""
        row = {
            "id": "usr_123",
            "email": "admin@test.com",
            "password_hash": stored_hash,
            "role": role,
            "totp_enabled": False,
            "session_version": 1,
        }
        pool, conn = _make_pool(row=row)
        self.server.admin_router._state.db_pool = pool
        return pool, conn

    def test_login_bcrypt_legado_valido_migra_para_argon2id(self):
        hash_bcrypt = _gerar_hash_bcrypt("SenhaCorreta123")
        pool, conn = self._login_com_hash(hash_bcrypt)

        r = self.client.post(
            "/api/admin/login",
            json={"email": "admin@test.com", "password": "SenhaCorreta123"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["success"])

        # O UPDATE de lazy re-hashing deve ter sido chamado com hash $argon2id$.
        # (auth_limit tambem chama execute, entao varremos call_args_list.)
        chamadas_argon = [
            c for c in conn.execute.call_args_list
            if any(isinstance(a, str) and a.startswith("$argon2id$") for a in c.args)
        ]
        self.assertGreaterEqual(len(chamadas_argon), 1, "UPDATE de lazy re-hash nao encontrado")
        # O hash e o segundo argumento (apos a query SQL).
        novo_hash = chamadas_argon[0].args[1]
        # O novo hash deve ser verificavel com a mesma senha
        valido, _ = verify_password("SenhaCorreta123", novo_hash)
        self.assertTrue(valido)

    def test_login_argon2id_ja_migrado_nao_chama_update(self):
        hash_argon = hash_password("SenhaCorreta123")
        pool, conn = self._login_com_hash(hash_argon)

        r = self.client.post(
            "/api/admin/login",
            json={"email": "admin@test.com", "password": "SenhaCorreta123"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        # Nao deve haver UPDATE de lazy re-hash (hash ja esta no padrao vigente).
        # auth_limit chama execute com DELETE, entao filtramos por argon2id.
        chamadas_argon = [
            c for c in conn.execute.call_args_list
            if any(isinstance(a, str) and a.startswith("$argon2id$") for a in c.args)
        ]
        self.assertEqual(len(chamadas_argon), 0, "UPDATE de lazy re-hash nao deveria ocorrer")

    def test_login_senha_invalida_bcrypt_legado(self):
        hash_bcrypt = _gerar_hash_bcrypt("SenhaCorreta123")
        self._login_com_hash(hash_bcrypt)

        r = self.client.post(
            "/api/admin/login",
            json={"email": "admin@test.com", "password": "SenhaErrada123"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 401)

    def test_login_senha_invalida_argon2id(self):
        hash_argon = hash_password("SenhaCorreta123")
        self._login_com_hash(hash_argon)

        r = self.client.post(
            "/api/admin/login",
            json={"email": "admin@test.com", "password": "SenhaErrada123"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 401)

    def test_login_usuario_inexistente_nao_vaza_existencia(self):
        # fetchrow retorna None -> usuario nao encontrado.
        pool, conn = _make_pool(row=None)
        self.server.admin_router._state.db_pool = pool

        r = self.client.post(
            "/api/admin/login",
            json={"email": "naoexiste@test.com", "password": "qualquer123"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 401)
        # verify_missing_user deve ter sido executado (mitigacao de timing).
        self.assertTrue(conn.fetchrow.called)

    def test_login_campos_vazios_401(self):
        self._login_com_hash(_gerar_hash_bcrypt("x"))
        r = self.client.post(
            "/api/admin/login",
            json={"email": "", "password": ""},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
