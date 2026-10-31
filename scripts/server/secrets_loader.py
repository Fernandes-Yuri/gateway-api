"""Carregador hibrido de segredos via AWS SSM Parameter Store + KMS (SPEC-11).

Implementa a leitura resiliente de segredos de producao a partir do AWS Systems
Manager (SSM) Parameter Store sob o prefixo `/braga-saude/production/*` com
descriptografia automatica via AWS KMS (`WithDecryption=True`), mantendo cache
em memoria no processo Gunicorn/Uvicorn e fallback transparente para `.env` e
`os.environ` em desenvolvimento local e CI.

Regras de seguranca (SPEC-11):
  - Em producao na EC2 (`USE_AWS_SECRETS=1`), os segredos sao obtidos via IAM
    Instance Profile (`ssm:GetParametersByPath` + `kms:Decrypt`) e mantidos
    exclusivamente em memoria (`os.environ` / `/run` tmpfs), sem persistir
    credenciais em disco.
  - Em desenvolvimento local e CI (`USE_AWS_SECRETS != "1"`), o carregador nao
    realiza chamadas de rede para a AWS e faz fallback gracioso para variaveis
    de ambiente e arquivos `.env` locais.
  - Valores de segredos nunca sao exibidos em logs ou mensagens de erro.
"""
import argparse
import logging
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore[assignment]
    load_dotenv = None  # type: ignore[assignment]

logger = logging.getLogger("braga.secrets_loader")

DEFAULT_SSM_PREFIX = "/braga-saude/production"
DEFAULT_AWS_REGION = "us-east-2"

# Hierarquia canonica de parametros definida na SPEC-11 §2.
PARAMETER_TYPES: dict[str, str] = {
    "DATABASE_URL": "SecureString",
    "MIGRATIONS_DATABASE_URL": "SecureString",
    "AUTH_PEPPER_SECRET": "SecureString",
    "SERVICE_TOKEN": "SecureString",
    "ADMIN_TOTP_SECRET": "SecureString",
    "GROQ_API_KEY": "SecureString",
    "GEMINI_API_KEY": "SecureString",
    "PUBLIC_API_BASE_URL": "String",
}
KNOWN_SECRET_KEYS: tuple[str, ...] = tuple(PARAMETER_TYPES.keys())

AWS_SSM_PREFIX = os.getenv("AWS_SSM_PREFIX", DEFAULT_SSM_PREFIX)
USE_AWS_SECRETS = os.getenv("USE_AWS_SECRETS", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
)

# Cache em memoria por worker Gunicorn/Uvicorn e instancia reutilizavel do client.
_SECRETS_CACHE: dict[str, str] = {}
_LOADED: bool = False
_LAST_SOURCE: str = "uninitialized"
_SSM_CLIENT: Any = None
_SSM_CLIENT_REGION: str | None = None

_BOTO_CONFIG = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)


def is_aws_secrets_enabled() -> bool:
    """Retorna True quando o carregamento via AWS SSM esta habilitado."""
    return os.getenv("USE_AWS_SECRETS", "0").strip() in (
        "1",
        "true",
        "True",
        "yes",
    )


def _resolve_region(region: str | None = None) -> str:
    """Resolve a regiao AWS prioritaria para o Parameter Store."""
    return (
        region
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_AWS_REGION
    )


def _normalize_prefix(prefix: str | None = None) -> str:
    """Normaliza o prefixo SSM garantindo barra inicial e sem barra dupla."""
    raw = (prefix or os.getenv("AWS_SSM_PREFIX") or DEFAULT_SSM_PREFIX).strip()
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.rstrip("/")


def get_ssm_client(region: str | None = None) -> Any:
    """Retorna instancia reutilizavel e thread-safe do client SSM."""
    global _SSM_CLIENT, _SSM_CLIENT_REGION
    resolved_region = _resolve_region(region)
    if _SSM_CLIENT is None or _SSM_CLIENT_REGION != resolved_region:
        _SSM_CLIENT = boto3.client(
            "ssm",
            region_name=resolved_region,
            config=_BOTO_CONFIG,
        )
        _SSM_CLIENT_REGION = resolved_region
    return _SSM_CLIENT


def clear_secrets_cache() -> None:
    """Limpa o cache em memoria e reinicia o client SSM (util para testes)."""
    global _LOADED, _LAST_SOURCE, _SSM_CLIENT, _SSM_CLIENT_REGION
    _SECRETS_CACHE.clear()
    _LOADED = False
    _LAST_SOURCE = "uninitialized"
    _SSM_CLIENT = None
    _SSM_CLIENT_REGION = None


def get_last_source() -> str:
    """Retorna a fonte do ultimo carregamento ('ssm', 'env' ou 'uninitialized')."""
    return _LAST_SOURCE


def _default_dotenv_candidates() -> list[Path]:
    """Lista os caminhos candidatos de `.env` para fallback local."""
    server_dir = Path(__file__).resolve().parent
    repo_root = server_dir.parent.parent
    candidates = [
        Path("/run/braga-saude/gateway.env"),
        repo_root / ".env",
        server_dir / ".env",
        Path("/home/ec2-user/braga-gateway/.env"),
        Path("/home/ec2-user/braga-gateway/app/.env"),
    ]
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Extrai pares chave=valor de um arquivo `.env` sem expor valores em log."""
    if not path.is_file():
        return {}
    if dotenv_values is not None:
        raw = dotenv_values(path)
        return {k: v for k, v in raw.items() if k and v is not None}

    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            parsed[key] = val
    return parsed


def _load_dotenv_fallback(
    dotenv_paths: Iterable[str | Path] | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """Carrega variaveis de arquivos `.env` locais e sincroniza com `os.environ`."""
    candidates = (
        [Path(p) for p in dotenv_paths]
        if dotenv_paths is not None
        else _default_dotenv_candidates()
    )
    loaded: dict[str, str] = {}
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if load_dotenv is not None:
            load_dotenv(candidate, override=override)
        file_vars = _parse_dotenv_file(candidate)
        for key, value in file_vars.items():
            if override or key not in os.environ:
                os.environ[key] = value
            loaded[key] = os.environ.get(key, value)

    for key in KNOWN_SECRET_KEYS:
        env_val = os.environ.get(key)
        if env_val:
            loaded[key] = env_val

    return loaded


def fetch_ssm_parameters(
    prefix: str | None = None,
    *,
    region: str | None = None,
    ssm_client: Any = None,
) -> dict[str, str]:
    """Consulta o AWS SSM Parameter Store com paginacao e descriptografia KMS.

    Usa o paginador oficial `get_parameters_by_path` com `Recursive=True` e
    `WithDecryption=True`. Excecoes do SDK (`ClientError`, `BotoCoreError`)
    propagam para que o chamador decida a politica de fallback.
    """
    normalized_prefix = _normalize_prefix(prefix)
    path_with_slash = f"{normalized_prefix}/"
    client = ssm_client if ssm_client is not None else get_ssm_client(region)

    parameters: dict[str, str] = {}
    paginator = client.get_paginator("get_parameters_by_path")
    page_iterator = paginator.paginate(
        Path=path_with_slash,
        Recursive=True,
        WithDecryption=True,
    )

    for page in page_iterator:
        for item in page.get("Parameters", []):
            raw_name = item.get("Name", "")
            value = item.get("Value")
            if not raw_name or value is None:
                continue
            if raw_name.startswith(path_with_slash):
                env_key = raw_name[len(path_with_slash):].strip("/")
            else:
                env_key = raw_name.rsplit("/", 1)[-1].strip()
            if env_key:
                parameters[env_key] = value

    return parameters


def load_secrets(
    *,
    prefix: str | None = None,
    region: str | None = None,
    dotenv_paths: Iterable[str | Path] | None = None,
    ssm_client: Any = None,
    force_reload: bool = False,
    override: bool = False,
    strict: bool = False,
) -> dict[str, str]:
    """Carrega segredos em memoria com cache por processo e fallback seguro.

    Fluxo:
      1. Se `USE_AWS_SECRETS != "1"`, usa o fallback `.env` / `os.environ`.
      2. Se `USE_AWS_SECRETS == "1"`, consulta o AWS SSM Parameter Store
         (`/braga-saude/production/*`) com KMS (`WithDecryption=True`),
         popula `_SECRETS_CACHE` e injeta em `os.environ`.
      3. Em caso de falha de rede/permissao no SSM (`ClientError`,
         `BotoCoreError`, `OSError`) quando `strict=False`, registra aviso
         sem vazar segredos e aciona o fallback `.env` / `os.environ`.
    """
    global _LOADED, _LAST_SOURCE, USE_AWS_SECRETS, AWS_SSM_PREFIX

    USE_AWS_SECRETS = is_aws_secrets_enabled()
    AWS_SSM_PREFIX = _normalize_prefix(prefix)

    if _LOADED and not force_reload:
        for key, value in _SECRETS_CACHE.items():
            if override or not os.environ.get(key):
                os.environ[key] = value
        return dict(_SECRETS_CACHE)

    if not USE_AWS_SECRETS:
        fallback_data = _load_dotenv_fallback(dotenv_paths, override=override)
        _SECRETS_CACHE.clear()
        _SECRETS_CACHE.update(fallback_data)
        _LOADED = True
        _LAST_SOURCE = "env"
        return dict(_SECRETS_CACHE)

    try:
        ssm_data = fetch_ssm_parameters(
            AWS_SSM_PREFIX,
            region=region,
            ssm_client=ssm_client,
        )
        if not ssm_data:
            logger.warning(
                "[secrets_loader] Nenhum parametro retornado do SSM em %s; "
                "acionando fallback local (.env / os.environ).",
                AWS_SSM_PREFIX,
            )
            fallback_data = _load_dotenv_fallback(dotenv_paths, override=override)
            _SECRETS_CACHE.clear()
            _SECRETS_CACHE.update(fallback_data)
            _LOADED = True
            _LAST_SOURCE = "env"
            return dict(_SECRETS_CACHE)

        _SECRETS_CACHE.clear()
        _SECRETS_CACHE.update(ssm_data)
        for key, value in ssm_data.items():
            if override or not os.environ.get(key):
                os.environ[key] = value
        _LOADED = True
        _LAST_SOURCE = "ssm"
        logger.info(
            "[secrets_loader] %d parametros carregados do AWS SSM (%s).",
            len(ssm_data),
            AWS_SSM_PREFIX,
        )
        return dict(_SECRETS_CACHE)
    except (ClientError, BotoCoreError, OSError) as exc:
        if strict:
            raise
        logger.warning(
            "[secrets_loader] Falha ao consultar AWS SSM (%s): %s. "
            "Acionando fallback seguro para .env / os.environ.",
            AWS_SSM_PREFIX,
            type(exc).__name__,
        )
        fallback_data = _load_dotenv_fallback(dotenv_paths, override=override)
        _SECRETS_CACHE.clear()
        _SECRETS_CACHE.update(fallback_data)
        _LOADED = True
        _LAST_SOURCE = "env"
        return dict(_SECRETS_CACHE)


def get_secret(key: str, default: str | None = None) -> str | None:
    """Retorna um segredo do cache em memoria ou do `os.environ`."""
    if not _LOADED:
        load_secrets()
    if key in _SECRETS_CACHE and _SECRETS_CACHE[key]:
        return _SECRETS_CACHE[key]
    return os.environ.get(key, default)


def ensure_ssm_secret(
    name: str,
    value: str,
    *,
    param_type: str | None = None,
    prefix: str | None = None,
    kms_key_id: str | None = None,
    region: str | None = None,
    ssm_client: Any = None,
    overwrite: bool = False,
) -> bool:
    """Provisiona idempotentemente um parametro no AWS SSM Parameter Store.

    Retorna True se o parametro foi criado/atualizado, ou False se ja existia
    no Parameter Store (`ParameterAlreadyExists`) com `overwrite=False`.
    """
    normalized_prefix = _normalize_prefix(prefix)
    clean_name = name.strip("/")
    full_name = f"{normalized_prefix}/{clean_name}"
    resolved_type = param_type or PARAMETER_TYPES.get(clean_name, "SecureString")
    resolved_kms = kms_key_id or os.getenv("AWS_KMS_KEY_ID")
    client = ssm_client if ssm_client is not None else get_ssm_client(region)

    kwargs: dict[str, Any] = {
        "Name": full_name,
        "Value": value,
        "Type": resolved_type,
        "Overwrite": overwrite,
    }
    if resolved_type == "SecureString" and resolved_kms:
        kwargs["KeyId"] = resolved_kms

    already_exists_exc = getattr(
        getattr(client, "exceptions", None),
        "ParameterAlreadyExists",
        None,
    )
    is_typed_match = (
        isinstance(already_exists_exc, type)
        and issubclass(already_exists_exc, BaseException)
    )
    try:
        client.put_parameter(**kwargs)
        return True
    except ClientError as exc:
        if (
            not overwrite
            and (
                (is_typed_match and isinstance(exc, already_exists_exc))
                or exc.response.get("Error", {}).get("Code") == "ParameterAlreadyExists"
            )
        ):
            return False
        raise


def ensure_pepper_secret(
    *,
    prefix: str | None = None,
    kms_key_id: str | None = None,
    region: str | None = None,
    ssm_client: Any = None,
) -> str:
    """Garante que `AUTH_PEPPER_SECRET` exista em memoria e no SSM (sem disco)."""
    existing = os.environ.get("AUTH_PEPPER_SECRET", "").strip() or _SECRETS_CACHE.get(
        "AUTH_PEPPER_SECRET", ""
    ).strip()
    if existing:
        os.environ["AUTH_PEPPER_SECRET"] = existing
        _SECRETS_CACHE["AUTH_PEPPER_SECRET"] = existing
        return existing

    generated = secrets.token_hex(32)
    if is_aws_secrets_enabled():
        try:
            ensure_ssm_secret(
                "AUTH_PEPPER_SECRET",
                generated,
                param_type="SecureString",
                prefix=prefix,
                kms_key_id=kms_key_id,
                region=region,
                ssm_client=ssm_client,
                overwrite=False,
            )
            refreshed = fetch_ssm_parameters(
                prefix,
                region=region,
                ssm_client=ssm_client,
            )
            if refreshed.get("AUTH_PEPPER_SECRET"):
                generated = refreshed["AUTH_PEPPER_SECRET"]
        except (ClientError, BotoCoreError, OSError) as exc:
            logger.warning(
                "[secrets_loader] Nao foi possivel persistir AUTH_PEPPER_SECRET no SSM (%s); "
                "mantendo valor efemero em memoria.",
                type(exc).__name__,
            )

    os.environ["AUTH_PEPPER_SECRET"] = generated
    _SECRETS_CACHE["AUTH_PEPPER_SECRET"] = generated
    return generated


def sync_env_file_to_ssm(
    env_file: str | Path,
    *,
    prefix: str | None = None,
    kms_key_id: str | None = None,
    region: str | None = None,
    ssm_client: Any = None,
) -> list[str]:
    """Sincroniza idempotentemente chaves conhecidas de um `.env` legado para o SSM."""
    path = Path(env_file)
    if not path.is_file():
        return []

    parsed = _parse_dotenv_file(path)
    synced_keys: list[str] = []
    for key in KNOWN_SECRET_KEYS:
        val = parsed.get(key, "").strip()
        if not val:
            continue
        ensure_ssm_secret(
            key,
            val,
            param_type=PARAMETER_TYPES.get(key, "SecureString"),
            prefix=prefix,
            kms_key_id=kms_key_id,
            region=region,
            ssm_client=ssm_client,
            overwrite=False,
        )
        synced_keys.append(key)
    return synced_keys


def _format_systemd_env_value(value: str) -> str:
    """Formata um valor para `EnvironmentFile` do systemd escapando aspas."""
    cleaned = value.replace("\r", "").replace("\n", "")
    if any(ch in cleaned for ch in (" ", "\t", '"', "'", "#", "$", "\\")):
        escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return cleaned


def write_runtime_env_file(
    target_path: str | Path,
    secrets_map: dict[str, str] | None = None,
    *,
    allow_non_tmpfs: bool = False,
) -> Path:
    """Escreve as variaveis carregadas em tmpfs (`/run` ou `/dev/shm`) com modo 0600.

    Por seguranca (SPEC-11), recusa caminhos fora de `/run` e `/dev/shm` a menos
    que `allow_non_tmpfs=True` seja informado explicitamente (ex.: testes com
    diretorio temporario controlado).
    """
    resolved = Path(target_path).resolve()
    if not allow_non_tmpfs and not (
        str(resolved).startswith("/run/") or str(resolved).startswith("/dev/shm/")
    ):
        raise ValueError(
            f"Caminho {resolved} nao reside em tmpfs (/run ou /dev/shm). "
            "Segredos nao devem ser gravados em disco persistente."
        )

    data = secrets_map if secrets_map is not None else load_secrets()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"{key}={_format_systemd_env_value(val)}"
        for key, val in sorted(data.items())
        if key and val is not None
    ]
    content = ("\n".join(lines) + "\n") if lines else ""

    fd, tmp_name = tempfile.mkstemp(
        prefix=".gateway_env_",
        dir=str(resolved.parent),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, resolved)
        os.chmod(resolved, 0o600)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    return resolved


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada CLI para provisionamento e injecao efemera no deploy."""
    parser = argparse.ArgumentParser(
        description="Carregador hibrido de segredos AWS SSM + KMS (SPEC-11)."
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("AWS_SSM_PREFIX", DEFAULT_SSM_PREFIX),
        help="Prefixo no AWS SSM Parameter Store (default: /braga-saude/production).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Regiao AWS (default: AWS_REGION / AWS_DEFAULT_REGION / us-east-2).",
    )
    parser.add_argument(
        "--sync-from-dotenv",
        metavar="PATH",
        help="Sincroniza chaves conhecidas de um .env legado para o SSM.",
    )
    parser.add_argument(
        "--ensure-pepper",
        action="store_true",
        help="Garante que AUTH_PEPPER_SECRET exista no SSM / memoria.",
    )
    parser.add_argument(
        "--write-runtime-env",
        metavar="RUNTIME_PATH",
        help="Escreve segredos carregados em arquivo efemero tmpfs (/run/...).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verifica o carregamento de segredos e exibe resumo nao sensivel.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Falha com codigo != 0 se o AWS SSM nao puder ser consultado.",
    )
    args = parser.parse_args(argv)

    try:
        if args.sync_from_dotenv:
            synced = sync_env_file_to_ssm(
                args.sync_from_dotenv,
                prefix=args.prefix,
                region=args.region,
            )
            print(
                f"[secrets_loader] {len(synced)} parametro(s) sincronizado(s) "
                f"de {args.sync_from_dotenv} para {args.prefix}."
            )

        secrets_map = load_secrets(
            prefix=args.prefix,
            region=args.region,
            force_reload=True,
            override=True,
            strict=args.strict,
        )

        if args.ensure_pepper:
            pepper = ensure_pepper_secret(
                prefix=args.prefix,
                region=args.region,
            )
            secrets_map["AUTH_PEPPER_SECRET"] = pepper

        if args.write_runtime_env:
            out_path = write_runtime_env_file(
                args.write_runtime_env,
                secrets_map,
            )
            print(
                f"[secrets_loader] Runtime efemero gravado em {out_path} "
                f"({len(secrets_map)} variaveis, fonte={get_last_source()})."
            )

        if args.check or not (
            args.sync_from_dotenv or args.ensure_pepper or args.write_runtime_env
        ):
            print(
                f"[secrets_loader] OK: {len(secrets_map)} variaveis disponiveis "
                f"(fonte={get_last_source()})."
            )
        return 0
    except (ClientError, BotoCoreError, OSError, ValueError) as exc:
        print(
            f"[secrets_loader] ERRO ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
