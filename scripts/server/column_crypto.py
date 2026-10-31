"""Motor criptografico de colunas a nivel de aplicacao e Blind Indexing (SPEC-12).

Implementa Criptografia a Nivel de Aplicacao (ALE - Application-Level Encryption)
para proteger PII e dados clinicos em repouso no PostgreSQL (RDS) contra dumps
nao autorizados (`pg_dump`) e injecoes SQL:

1. Cifragem Simetrica Autenticada (AES-256-GCM):
   - Algoritmo: AES/GCM/NoPadding (256 bits).
   - IV/Nonce: 12 bytes gerados por CSPRNG (`os.urandom(12)`) exclusivos por operacao.
   - Tag de Autenticacao: 16 bytes (128 bits) anexada ao ciphertext.
   - Formato persistido no banco:
     `enc:v1:<iv_b64>:<ciphertext_com_tag_b64>`

2. Blind Indexing (Busca O(1) sem Decifrar):
   - Algoritmo: `HMAC-SHA256(BLIND_INDEX_PEPPER, dado_normalizado)`.
   - Deterministico, unidirecional e truncado em 32 caracteres hexadecimais.

3. Envelope Encryption e Gestao de Chaves:
   - Suporta AWS KMS (`AWS_KMS_KEY_ID` / `DATA_ENCRYPTION_ENCRYPTED_DEK`) ou
     derivacao local/CI via HKDF-SHA256 sobre `DATA_ENCRYPTION_KEY`, garantindo
     execucao 100% deterministica e offline na suite de testes e CI.
   - Retrocompatibilidade defensiva (lazy encryption): valores legados em texto
     plano (sem o prefixo `enc:v1:`) sao retornados intactos na leitura.
"""
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import unicodedata
from typing import Any, Mapping, Optional, overload

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("braga.column_crypto")

ENCRYPTED_PREFIX = "enc:v1:"
IV_LENGTH_BYTES = 12
TAG_LENGTH_BYTES = 16
KEY_LENGTH_BYTES = 32
BLIND_INDEX_HEX_LENGTH = 32

_HKDF_SALT = b"braga-saude-spec12-ale-salt-v1"
_HKDF_INFO_DEK = b"braga-saude:column-crypto:aes-256-gcm:v1"
_HKDF_INFO_BINDEX = b"braga-saude:column-crypto:blind-index-hmac:v1"

# Semente de fallback estritamente para execucao local/CI quando nenhuma chave
# foi injetada no ambiente (mantem os testes legados 100% verdes sem rede).
_LOCAL_CI_FALLBACK_IKM = b"braga-saude-local-ci-fallback-dek-spec12-v1"
_LOCAL_CI_FALLBACK_PEPPER = b"braga-saude-local-ci-fallback-blind-pepper-spec12-v1"

_PHONE_CANDIDATE_RE = re.compile(r"^[\d\s\-\(\)\+\.]+$")

# Cache em memoria para DEK desencapsulada via AWS KMS quando ativo.
_KMS_DEK_CACHE: dict[str, bytes] = {}


class ColumnTamperError(InvalidTag, ValueError):
    """Erro disparado quando o payload `enc:v1:` foi adulterado ou e invalido.

    Herda simultaneamente de `cryptography.exceptions.InvalidTag` e `ValueError`
    para garantir captura uniforme tanto em blocos que tratam erros criptograficos
    quanto em validacoes de dominio.
    """


def clear_crypto_cache() -> None:
    """Limpa o cache em memoria de chaves desencapsuladas via KMS."""
    _KMS_DEK_CACHE.clear()


def derive_key_hkdf(
    master_secret: str | bytes,
    *,
    info: bytes = _HKDF_INFO_DEK,
    salt: bytes = _HKDF_SALT,
    length: int = KEY_LENGTH_BYTES,
) -> bytes:
    """Deriva uma subchave criptografica de 256 bits via HKDF-SHA256."""
    if isinstance(master_secret, str):
        ikm = master_secret.encode("utf-8")
    else:
        ikm = bytes(master_secret)
    if not ikm:
        raise ValueError("Chave mestra vazia nao permitida para derivacao HKDF.")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(ikm)


def unwrap_dek_with_kms(
    encrypted_dek_b64: str,
    *,
    kms_client: Any = None,
    region: str | None = None,
) -> bytes:
    """Desencapsula uma Data Encryption Key (DEK) cifrada pelo AWS KMS."""
    cache_key = encrypted_dek_b64.strip()
    if cache_key in _KMS_DEK_CACHE:
        return _KMS_DEK_CACHE[cache_key]

    ciphertext_blob = base64.b64decode(cache_key)
    if kms_client is None:
        import boto3

        resolved_region = (
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-2"
        )
        kms_client = boto3.client("kms", region_name=resolved_region)

    response = kms_client.decrypt(CiphertextBlob=ciphertext_blob)
    plaintext = response.get("Plaintext")
    if not plaintext:
        raise RuntimeError("AWS KMS nao retornou Plaintext ao desencapsular DEK.")

    dek = (
        bytes(plaintext)
        if len(plaintext) == KEY_LENGTH_BYTES
        else derive_key_hkdf(bytes(plaintext), info=_HKDF_INFO_DEK)
    )
    _KMS_DEK_CACHE[cache_key] = dek
    return dek


def generate_envelope_dek(
    kms_key_id: str,
    *,
    kms_client: Any = None,
    region: str | None = None,
) -> tuple[bytes, str]:
    """Gera um par (plaintext_dek, encrypted_dek_b64) via AWS KMS GenerateDataKey."""
    if kms_client is None:
        import boto3

        resolved_region = (
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-2"
        )
        kms_client = boto3.client("kms", region_name=resolved_region)

    response = kms_client.generate_data_key(KeyId=kms_key_id, KeySpec="AES_256")
    plaintext_dek = bytes(response["Plaintext"])
    encrypted_b64 = base64.b64encode(bytes(response["CiphertextBlob"])).decode("ascii")
    _KMS_DEK_CACHE[encrypted_b64] = plaintext_dek
    return plaintext_dek, encrypted_b64


def _resolve_dek(*, kms_client: Any = None) -> bytes:
    """Resolve a chave simetrica de 32 bytes (256 bits) para AES-256-GCM.

    Ordem de precedencia (SPEC-12 §2.3):
      1. Envelope KMS (`USE_AWS_KMS_ENVELOPE=1` + `DATA_ENCRYPTION_ENCRYPTED_DEK`),
         com fallback automatico para HKDF local caso o KMS nao esteja ativo.
      2. Variavel de ambiente `DATA_ENCRYPTION_KEY` derivada via HKDF-SHA256.
      3. Fallback deterministico local/CI para execucao offline de testes.
    """
    use_kms = os.getenv("USE_AWS_KMS_ENVELOPE", "0").strip() in (
        "1",
        "true",
        "True",
        "yes",
    )
    encrypted_dek_b64 = os.getenv("DATA_ENCRYPTION_ENCRYPTED_DEK", "").strip()
    if use_kms and encrypted_dek_b64:
        try:
            return unwrap_dek_with_kms(encrypted_dek_b64, kms_client=kms_client)
        except Exception as exc:
            logger.warning(
                "[column_crypto] Falha ao desencapsular DEK no AWS KMS (%s); "
                "acionando fallback HKDF local.",
                type(exc).__name__,
            )

    raw_env_key = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    if raw_env_key:
        return derive_key_hkdf(raw_env_key, info=_HKDF_INFO_DEK)

    return derive_key_hkdf(_LOCAL_CI_FALLBACK_IKM, info=_HKDF_INFO_DEK)


def _resolve_blind_index_pepper() -> bytes:
    """Resolve o segredo Pepper usado no HMAC-SHA256 do Blind Index."""
    raw_pepper = os.getenv("BLIND_INDEX_PEPPER", "").strip()
    if raw_pepper:
        return raw_pepper.encode("utf-8")

    raw_env_key = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    if raw_env_key:
        return derive_key_hkdf(raw_env_key, info=_HKDF_INFO_BINDEX)

    return _LOCAL_CI_FALLBACK_PEPPER


def is_encrypted(value: Optional[str]) -> bool:
    """Retorna True se o valor possuir o prefixo canonico `enc:v1:`."""
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


@overload
def encrypt_field(plaintext: None) -> None: ...


@overload
def encrypt_field(plaintext: str) -> str: ...


def encrypt_field(plaintext: Optional[str]) -> Optional[str]:
    """Cifra um campo textual com AES-256-GCM no formato `enc:v1:<iv_b64>:<ct_b64>`.

    - Gera um IV/Nonce exclusivo de 12 bytes via CSPRNG (`os.urandom(12)`).
    - Anexa a tag de autenticacao GCM de 16 bytes (128 bits) ao final do ciphertext.
    - Idempotente: se o valor ja iniciar com `enc:v1:`, retorna sem recifrar.
    - Preserva `None` e string vazia `""`.
    """
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    if plaintext == "":
        return ""
    if is_encrypted(plaintext):
        return plaintext

    dek = _resolve_dek()
    iv = os.urandom(IV_LENGTH_BYTES)
    aesgcm = AESGCM(dek)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)

    iv_b64 = base64.b64encode(iv).decode("ascii")
    ct_b64 = base64.b64encode(ciphertext_with_tag).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{iv_b64}:{ct_b64}"


@overload
def decrypt_field(ciphertext: None) -> None: ...


@overload
def decrypt_field(ciphertext: str) -> str: ...


def decrypt_field(ciphertext: Optional[str]) -> Optional[str]:
    """Decifra um payload `enc:v1:<iv_b64>:<ct_b64>` validando a tag GCM de 16 bytes.

    Retrocompatibilidade (SPEC-12 §4.1 / §5):
      - Se `ciphertext` nao possuir o prefixo `enc:v1:`, assume-se dado legado em
        texto claro (lazy encryption) e retorna o valor original sem erro.
      - Se possuir o prefixo `enc:v1:` mas a estrutura, o IV, o ciphertext ou a
        tag GCM de 16 bytes tiverem sido adulterados (ou a chave for incorreta),
        levanta `ColumnTamperError` (`InvalidTag` / `ValueError`).
    """
    if ciphertext is None:
        return None
    if not isinstance(ciphertext, str):
        return ciphertext
    if ciphertext == "":
        return ""

    if not ciphertext.startswith(ENCRYPTED_PREFIX):
        return ciphertext

    parts = ciphertext.split(":")
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1":
        raise ColumnTamperError(
            "Formato de payload criptografado invalido "
            "(esperado enc:v1:<iv_b64>:<ciphertext_com_tag_b64>)."
        )

    iv_b64, ct_b64 = parts[2], parts[3]
    if not iv_b64 or not ct_b64:
        raise ColumnTamperError("Componentes Base64 vazios no payload criptografado.")

    try:
        iv = base64.b64decode(iv_b64, validate=True)
        ciphertext_with_tag = base64.b64decode(ct_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ColumnTamperError(
            "Codificacao Base64 corrompida no payload criptografado."
        ) from exc

    if len(iv) != IV_LENGTH_BYTES:
        raise ColumnTamperError(
            f"Tamanho de IV invalido: esperado {IV_LENGTH_BYTES} bytes, "
            f"recebido {len(iv)} bytes."
        )
    if len(ciphertext_with_tag) < TAG_LENGTH_BYTES:
        raise ColumnTamperError(
            f"Payload truncado: menor que a tag GCM de {TAG_LENGTH_BYTES} bytes."
        )

    dek = _resolve_dek()
    aesgcm = AESGCM(dek)
    try:
        plaintext_bytes = aesgcm.decrypt(iv, ciphertext_with_tag, None)
    except InvalidTag as exc:
        raise ColumnTamperError(
            "Falha de integridade AES-256-GCM: tag de autenticacao invalida "
            "ou dados adulterados."
        ) from exc

    try:
        return plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ColumnTamperError(
            "Bytes decifrados nao correspondem a UTF-8 valido."
        ) from exc


def normalize_for_blind_index(plaintext: str) -> str:
    """Normaliza um valor para indexacao cega deterministica.

    - Se o valor estiver cifrado (`enc:v1:...`), decifra antes de normalizar.
    - Aplica normalizacao Unicode NFKC, `strip()` e `lower()`.
    - Para telefones (compostos por digitos e pontuacao telefonica), mantem
      apenas os digitos para que `(11) 98888-7777` e `11988887777` gerem o
      mesmo blind index.
    """
    if is_encrypted(plaintext):
        plaintext = decrypt_field(plaintext)

    normalized = unicodedata.normalize("NFKC", plaintext).strip().lower()
    if not normalized:
        return ""

    if _PHONE_CANDIDATE_RE.match(normalized) and any(ch.isdigit() for ch in normalized):
        digits = re.sub(r"\D", "", normalized)
        if digits:
            return digits

    return " ".join(normalized.split())


@overload
def compute_blind_index(plaintext: None) -> None: ...


@overload
def compute_blind_index(plaintext: str) -> str: ...


def compute_blind_index(plaintext: Optional[str]) -> Optional[str]:
    """Calcula o Blind Index `HMAC-SHA256(BLIND_INDEX_PEPPER, dado_normalizado)`
    truncado em 32 caracteres hexadecimais (SPEC-12 §2.2).
    """
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)

    normalized = normalize_for_blind_index(plaintext)
    if not normalized:
        return ""

    pepper = _resolve_blind_index_pepper()
    digest = hmac.new(pepper, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:BLIND_INDEX_HEX_LENGTH]


def encrypt_with_blind_index(
    plaintext: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Retorna `(valor_cifrado, blind_index)` pronto para persistencia no RDS."""
    if plaintext is None:
        return None, None
    cleaned = plaintext.strip() if isinstance(plaintext, str) else str(plaintext).strip()
    if not cleaned:
        return "", None
    plain_val = decrypt_field(cleaned) if is_encrypted(cleaned) else cleaned
    return encrypt_field(plain_val), compute_blind_index(plain_val)


def decrypt_emergency_contact_row(
    row: Mapping[str, Any] | dict[str, Any] | None,
) -> Optional[dict[str, Any]]:
    """Decifra transparentemente uma linha de `emergency_contacts`."""
    if row is None:
        return None
    data = dict(row)
    raw_encrypted = data.get("phone_encrypted")
    raw_legacy = data.get("phone")
    if raw_encrypted:
        data["phone"] = decrypt_field(raw_encrypted)
    elif raw_legacy:
        data["phone"] = decrypt_field(raw_legacy)
    return data


async def upsert_emergency_contact(
    conn: Any,
    *,
    user_id: str,
    phone: str,
    name: Optional[str] = None,
    relation: Optional[str] = None,
    is_primary: bool = True,
) -> dict[str, Any]:
    """Persiste um contato de emergencia com `phone_encrypted` e `phone_bindex`."""
    phone_encrypted, phone_bindex = encrypt_with_blind_index(phone)
    row = await conn.fetchrow(
        """INSERT INTO emergency_contacts
               (user_id, name, relation, phone, phone_encrypted, phone_bindex, is_primary, updated_at)
           VALUES ($1, $2, $3, NULL, $4, $5, $6, NOW())
           RETURNING id, user_id, name, relation, phone, phone_encrypted, phone_bindex, is_primary, created_at, updated_at""",
        user_id,
        name,
        relation,
        phone_encrypted,
        phone_bindex,
        is_primary,
    )
    if row is None:
        return {
            "user_id": user_id,
            "name": name,
            "relation": relation,
            "phone": decrypt_field(phone_encrypted) if phone_encrypted else None,
            "phone_encrypted": phone_encrypted,
            "phone_bindex": phone_bindex,
            "is_primary": is_primary,
        }
    return decrypt_emergency_contact_row(row) or {}


async def find_emergency_contacts_by_phone(
    conn: Any,
    phone: str,
) -> list[dict[str, Any]]:
    """Busca instantanea O(1) em `emergency_contacts` via `phone_bindex` (SPEC-12 §2.2)."""
    bindex = compute_blind_index(phone)
    if not bindex:
        return []
    rows = await conn.fetch(
        "SELECT * FROM emergency_contacts WHERE phone_bindex = $1",
        bindex,
    )
    return [decrypt_emergency_contact_row(r) for r in rows if r is not None]  # type: ignore[misc]


__all__ = [
    "BLIND_INDEX_HEX_LENGTH",
    "ColumnTamperError",
    "ENCRYPTED_PREFIX",
    "IV_LENGTH_BYTES",
    "KEY_LENGTH_BYTES",
    "TAG_LENGTH_BYTES",
    "clear_crypto_cache",
    "compute_blind_index",
    "decrypt_emergency_contact_row",
    "decrypt_field",
    "derive_key_hkdf",
    "encrypt_field",
    "encrypt_with_blind_index",
    "find_emergency_contacts_by_phone",
    "generate_envelope_dek",
    "is_encrypted",
    "normalize_for_blind_index",
    "unwrap_dek_with_kms",
    "upsert_emergency_contact",
]
