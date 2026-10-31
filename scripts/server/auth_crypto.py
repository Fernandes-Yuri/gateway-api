"""Motor criptografico hybrido para senhas administrativas (SPEC-10).

Implementa a transicao de Bcrypt para **Argon2id (RFC 9106)** combinado com
uma camada de **Pepper secreto** (HMAC-SHA256 como pre-hash), mantendo
compatibilidade retroativa com hashes Bcrypt legados ($2a$, $2b$, $2y$) via
lazy re-hashing transparente no login.

Parametros calibrados para EC2 t3.small (2 vCPU x86_64, 2 GB RAM) —
SPEC-10 §3:
  - Variante: Argon2id
  - m_cost / memory_cost: 65536 KiB (64 MB)
  - t_cost / time_cost: 3 iteracoes (~120 ms por verificacao)
  - parallelism: 2 threads (alinhado aos 2 vCPUs)
  - salt_len: 16 bytes (128 bits, CSPRNG os.urandom)
  - hash_len: 32 bytes (256 bits)

Seguranca:
  - O pepper (AUTH_PEPPER_SECRET) nunca e persistido em banco, logs nem
    exposto em respostas de erro.
  - Usuarios inexistentes disparam um Argon2id dummy pre-computado para
    igualar o tempo de resposta ao caminho de sucesso (mitigacao de timing
    side-channel na enumeracao de e-mails).
"""
import hashlib
import hmac
import os
from typing import Optional, Tuple

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, VerificationError
import bcrypt

# ---------------------------------------------------------------------------
# Calibracao Argon2id para t3.small (SPEC-10 §3)
# ---------------------------------------------------------------------------
hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,      # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,           # Argon2id (padrao do PasswordHasher)
)

# Pepper secreto injetado via variavel de ambiente. String vazia se nao
# configurado — todas as funcoes falham fechado (RuntimeError) quando ausente.
PEPPER = os.environ.get("AUTH_PEPPER_SECRET", "").encode("utf-8")

# ---------------------------------------------------------------------------
# Hash dummy Argon2id (nao corresponde a nenhuma conta real)
# ---------------------------------------------------------------------------
# Usado exclusivamente no caminho "usuario nao encontrado" para gastar o
# mesmo tempo de Argon2id (~120 ms) que uma verificacao real falharia,
# impedindo enumeracao de e-mails por timing side-channel. Gerado com o
# proprio hasher para garantir ser um hash VALIDO (nao estoura excecao).
_DUMMY_ARGON2ID_HASH = hasher.hash(b"\x00" * 32)


def _pepper_password(password: str) -> bytes:
    """Aplica HMAC-SHA256 com o Pepper secreto como pre-hash da senha.

    Evita problemas de concatencao direta (estouro de 72 bytes do Bcrypt e
    ataques de extensao de comprimento). O pepper nunca cruza para o banco.
    """
    if not PEPPER:
        raise RuntimeError(
            "AUTH_PEPPER_SECRET nao configurada no ambiente. "
            "Defina a variavel antes de usar auth_crypto."
        )
    return hmac.new(PEPPER, password.encode("utf-8"), hashlib.sha256).digest()


def hash_password(password: str) -> str:
    """Gera hash moderno Argon2id + Pepper para uma senha nova.

    Usado em cadastro (admin_register) e redefinicao de senha
    (admin_password_reset). Novas contas nascem diretamente em
    $argon2id$ (SPEC-10 DoD item 2).
    """
    peppered = _pepper_password(password)
    return hasher.hash(peppered)


def verify_password(
    password: str, stored_hash: Optional[str]
) -> Tuple[bool, bool]:
    """Verifica a senha contra o hash gravado.

    Retorna (senha_valida, precisa_rehash):
      - senha_valida: True se a senha bate com o hash armazenado.
      - precisa_rehash: True quando o hash deve ser regenerado:
          * hash Bcrypt legado ($2a$/$2b$/$2y$) e senha correta ->
            dispara lazy re-hashing para Argon2id + Pepper;
          * hash Argon2id cujos parametros nao estao mais vigentes
            (check_needs_rehash).

    Fluxo de lazy migration (SPEC-10 §5):
      1. Usuario com hash Bcrypt legada faz login com senha correta
         -> (True, True)  => rota atualiza o banco para $argon2id$.
      2. Proximo login ja usa $argon2id$ -> (True, False).
    """
    if not stored_hash:
        return False, False

    # ---- 1. Caso Bcrypt Legado ($2a$, $2b$, $2y$) ---------------------
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            valid = bcrypt.checkpw(
                password.encode("utf-8"), stored_hash.encode("utf-8")
            )
            # Se valida, precisa de rehash imediato (lazy migration).
            return valid, valid
        except Exception:
            # Hash malformado: credencial invalida, sem estourar 500.
            return False, False

    # ---- 2. Caso Argon2id Moderno ($argon2id$) ------------------------
    if stored_hash.startswith("$argon2id$"):
        peppered = _pepper_password(password)
        try:
            valid = hasher.verify(stored_hash, peppered)
        except (VerifyMismatchError, VerificationError):
            return False, False
        # Verifica se os parametros do hash ainda sao os vigentes.
        needs_rehash = hasher.check_needs_rehash(stored_hash)
        return valid, needs_rehash

    # Formato desconhecido: credencial invalida.
    return False, False


def verify_missing_user(password: str) -> None:
    """Simula uma verificacao Argon2id para usuario inexistente.

    Gasta o mesmo tempo criptografico (HMAC-SHA256 + Argon2id) de um login
    real para que o tempo de resposta seja indistinguivel do caminho de
    sucesso, mitigando enumeracao de e-mails por timing side-channel.
    Sempre retorna sem efeito (senha invalida por definicao).
    """
    if not PEPPER:
        # Sem pepper nao ha caminho seguro: falha fechado silenciosamente.
        return
    dummy_peppered = _pepper_password(password)
    try:
        hasher.verify(_DUMMY_ARGON2ID_HASH, dummy_peppered)
    except (VerifyMismatchError, VerificationError):
        # O hash dummy nunca corresponde a senha real — falha esperada.
        pass
