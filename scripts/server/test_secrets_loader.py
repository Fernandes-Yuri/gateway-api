"""Suite de testes automatizados para SPEC-11 (Gestao de Segredos AWS SSM + KMS).

Valida:
  1. Carregamento de segredos do AWS SSM Parameter Store (/braga-saude/production/*)
     com paginacao e descriptografia KMS (WithDecryption=True).
  2. Cache em memoria por worker Gunicorn/Uvicorn (evita chamadas repetidas ao SSM).
  3. Fallback automatico e silencioso para .env / os.environ quando USE_AWS_SECRETS != "1".
  4. Resiliencia e fallback gracioso em erros de rede/permissao (ClientError,
     EndpointConnectionError, NoCredentialsError, OSError) e modo estrito (strict=True).
  5. Provisionamento idempotente no SSM com KMS (ensure_ssm_secret, ensure_pepper_secret,
     sync_env_file_to_ssm) e geracao de arquivo efemero apenas em tmpfs (/run ou /dev/shm).
  6. Integracao com config.py, deploy.sh e unidade systemd sem expor segredos em disco.
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError  # noqa: E402

import config  # noqa: E402
import secrets_loader  # noqa: E402


def _make_client_error(code: str, message: str = "Simulated AWS error") -> ClientError:
    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": "req-test-11"},
        },
        operation_name="GetParametersByPath",
    )


def _make_mock_ssm_client(pages: list[dict] | None = None, paginate_side_effect=None) -> MagicMock:
    client = MagicMock()
    paginator = MagicMock()
    if paginate_side_effect is not None:
        paginator.paginate.side_effect = paginate_side_effect
    else:
        paginator.paginate.return_value = pages or []
    client.get_paginator.return_value = paginator
    return client


class TestSecretsLoaderSSMAndFallback(unittest.TestCase):
    def setUp(self):
        secrets_loader.clear_secrets_cache()
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        secrets_loader.clear_secrets_cache()

    def test_load_secrets_from_ssm_with_pagination_and_kms_decryption(self):
        """Carrega todas as chaves da SPEC-11 em multiplas paginas com WithDecryption=True."""
        pages = [
            {
                "Parameters": [
                    {"Name": "/braga-saude/production/DATABASE_URL", "Value": "postgresql://app:sec@127.0.0.1:5432/db", "Type": "SecureString"},
                    {"Name": "/braga-saude/production/MIGRATIONS_DATABASE_URL", "Value": "postgresql://mig:sec@127.0.0.1:5432/db", "Type": "SecureString"},
                    {"Name": "/braga-saude/production/AUTH_PEPPER_SECRET", "Value": "a" * 64, "Type": "SecureString"},
                    {"Name": "/braga-saude/production/SERVICE_TOKEN", "Value": "svc_prod_token", "Type": "SecureString"},
                ]
            },
            {
                "Parameters": [
                    {"Name": "/braga-saude/production/ADMIN_TOTP_SECRET", "Value": "JBSWY3DPEHPK3PXP", "Type": "SecureString"},
                    {"Name": "/braga-saude/production/GROQ_API_KEY", "Value": "gsk_prod_123", "Type": "SecureString"},
                    {"Name": "/braga-saude/production/GEMINI_API_KEY", "Value": "gem_prod_456", "Type": "SecureString"},
                    {"Name": "/braga-saude/production/PUBLIC_API_BASE_URL", "Value": "https://api.bragasaude.com.br", "Type": "String"},
                ]
            },
        ]
        mock_ssm = _make_mock_ssm_client(pages=pages)

        with patch.dict(os.environ, {"USE_AWS_SECRETS": "1", "AWS_SSM_PREFIX": "/braga-saude/production"}, clear=True):
            result = secrets_loader.load_secrets(ssm_client=mock_ssm, force_reload=True, override=True)

            mock_ssm.get_paginator.assert_called_once_with("get_parameters_by_path")
            mock_ssm.get_paginator.return_value.paginate.assert_called_once_with(
                Path="/braga-saude/production/",
                Recursive=True,
                WithDecryption=True,
            )
            self.assertEqual(secrets_loader.get_last_source(), "ssm")
            self.assertEqual(len(result), 8)
            for key in secrets_loader.KNOWN_SECRET_KEYS:
                self.assertIn(key, result)
                self.assertEqual(os.environ.get(key), result[key])

            # Segunda chamada deve retornar do cache em memoria sem consultar o SSM novamente
            cached = secrets_loader.load_secrets(ssm_client=mock_ssm)
            self.assertEqual(cached, result)
            self.assertEqual(mock_ssm.get_paginator.call_count, 1)

    def test_fallback_to_dotenv_when_aws_secrets_disabled(self):
        """Quando USE_AWS_SECRETS != '1', nao toca no boto3 e carrega do .env local."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_file = Path(tmpdir) / ".env"
            dotenv_file.write_text(
                "DATABASE_URL=postgresql://local:local@127.0.0.1:5432/localdb\n"
                "GROQ_API_KEY=gsk_local_dev\n"
                "PUBLIC_API_BASE_URL=http://localhost:8080\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"USE_AWS_SECRETS": "0"}, clear=True), \
                    patch("secrets_loader.boto3.client") as mock_boto_client:
                loaded = secrets_loader.load_secrets(
                    dotenv_paths=[dotenv_file],
                    force_reload=True,
                    override=True,
                )
                mock_boto_client.assert_not_called()
                self.assertEqual(secrets_loader.get_last_source(), "env")
                self.assertEqual(loaded["DATABASE_URL"], "postgresql://local:local@127.0.0.1:5432/localdb")
                self.assertEqual(os.environ["GROQ_API_KEY"], "gsk_local_dev")
                self.assertEqual(secrets_loader.get_secret("PUBLIC_API_BASE_URL"), "http://localhost:8080")

    def test_fallback_on_network_and_client_errors(self):
        """Erros de rede/KMS/credenciais no SSM acionam fallback seguro para .env sem quebrar."""
        errors = [
            _make_client_error("AccessDeniedException", "User is not authorized to perform kms:Decrypt"),
            EndpointConnectionError(endpoint_url="https://ssm.us-east-2.amazonaws.com"),
            NoCredentialsError(),
            OSError("Network unreachable"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_file = Path(tmpdir) / ".env"
            dotenv_file.write_text("DATABASE_URL=postgresql://fallback@127.0.0.1:5432/db\n", encoding="utf-8")

            for err in errors:
                with self.subTest(error=type(err).__name__):
                    secrets_loader.clear_secrets_cache()
                    mock_ssm = _make_mock_ssm_client(paginate_side_effect=err)
                    with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True):
                        loaded = secrets_loader.load_secrets(
                            dotenv_paths=[dotenv_file],
                            ssm_client=mock_ssm,
                            force_reload=True,
                            override=True,
                        )
                        self.assertEqual(secrets_loader.get_last_source(), "env")
                        self.assertEqual(loaded.get("DATABASE_URL"), "postgresql://fallback@127.0.0.1:5432/db")

    def test_strict_mode_raises_on_ssm_failure(self):
        """Com strict=True, falhas do SSM propagam para o chamador."""
        mock_ssm = _make_mock_ssm_client(
            paginate_side_effect=_make_client_error("AccessDeniedException")
        )
        with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True):
            with self.assertRaises(ClientError):
                secrets_loader.load_secrets(ssm_client=mock_ssm, force_reload=True, strict=True)

    def test_empty_ssm_response_falls_back_to_dotenv(self):
        """Se o prefixo SSM existir mas retornar vazio, usa o fallback local."""
        mock_ssm = _make_mock_ssm_client(pages=[{"Parameters": []}])
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_file = Path(tmpdir) / ".env"
            dotenv_file.write_text("SERVICE_TOKEN=svc_local_fallback\n", encoding="utf-8")
            with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True):
                loaded = secrets_loader.load_secrets(
                    dotenv_paths=[dotenv_file],
                    ssm_client=mock_ssm,
                    force_reload=True,
                    override=True,
                )
                self.assertEqual(secrets_loader.get_last_source(), "env")
                self.assertEqual(loaded.get("SERVICE_TOKEN"), "svc_local_fallback")


class TestProvisioningAndRuntimeTmpfs(unittest.TestCase):
    def setUp(self):
        secrets_loader.clear_secrets_cache()
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        secrets_loader.clear_secrets_cache()

    def test_ensure_ssm_secret_with_kms_and_idempotency(self):
        """Provisiona SecureString com KeyId KMS e trata ParameterAlreadyExists de forma idempotente."""
        mock_ssm = MagicMock()
        with patch.dict(os.environ, {"AWS_KMS_KEY_ID": "alias/braga-saude-kms"}, clear=True):
            created = secrets_loader.ensure_ssm_secret(
                "DATABASE_URL",
                "postgresql://prod",
                prefix="/braga-saude/production",
                ssm_client=mock_ssm,
            )
            self.assertTrue(created)
            mock_ssm.put_parameter.assert_called_once_with(
                Name="/braga-saude/production/DATABASE_URL",
                Value="postgresql://prod",
                Type="SecureString",
                Overwrite=False,
                KeyId="alias/braga-saude-kms",
            )

        # Se ja existir, retorna False sem estourar erro
        mock_ssm.put_parameter.side_effect = _make_client_error("ParameterAlreadyExists")
        already = secrets_loader.ensure_ssm_secret(
            "DATABASE_URL",
            "postgresql://prod",
            prefix="/braga-saude/production",
            ssm_client=mock_ssm,
        )
        self.assertFalse(already)

    def test_ensure_pepper_secret_generates_in_memory_and_ssm(self):
        """Gera AUTH_PEPPER_SECRET de 64 hex chars e salva no SSM sem tocar no disco."""
        mock_ssm = _make_mock_ssm_client(pages=[{"Parameters": []}])
        with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True):
            pepper = secrets_loader.ensure_pepper_secret(ssm_client=mock_ssm)
            self.assertEqual(len(pepper), 64)
            self.assertEqual(os.environ.get("AUTH_PEPPER_SECRET"), pepper)
            mock_ssm.put_parameter.assert_called_once()
            kwargs = mock_ssm.put_parameter.call_args.kwargs
            self.assertEqual(kwargs["Name"], "/braga-saude/production/AUTH_PEPPER_SECRET")
            self.assertEqual(kwargs["Type"], "SecureString")

    def test_sync_env_file_to_ssm_respects_parameter_types(self):
        """PUBLIC_API_BASE_URL sobe como String e demais segredos como SecureString."""
        mock_ssm = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "DATABASE_URL=postgresql://u:p@host/db\n"
                "PUBLIC_API_BASE_URL=https://api.bragasaude.com.br\n"
                "UNRELATED_VAR=ignorar\n",
                encoding="utf-8",
            )
            synced = secrets_loader.sync_env_file_to_ssm(
                env_path,
                prefix="/braga-saude/production",
                kms_key_id="alias/kms-key",
                ssm_client=mock_ssm,
            )
            self.assertEqual(synced, ["DATABASE_URL", "PUBLIC_API_BASE_URL"])
            calls = {c.kwargs["Name"]: c.kwargs for c in mock_ssm.put_parameter.call_args_list}
            self.assertEqual(calls["/braga-saude/production/DATABASE_URL"]["Type"], "SecureString")
            self.assertEqual(calls["/braga-saude/production/DATABASE_URL"]["KeyId"], "alias/kms-key")
            self.assertEqual(calls["/braga-saude/production/PUBLIC_API_BASE_URL"]["Type"], "String")
            self.assertNotIn("KeyId", calls["/braga-saude/production/PUBLIC_API_BASE_URL"])

    def test_write_runtime_env_file_rejects_persistent_disk_and_enforces_0600(self):
        """Recusa gravacao fora de /run ou /dev/shm por padrao e aplica permissao 0600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_target = Path(tmpdir) / "gateway.env"
            with self.assertRaises(ValueError):
                secrets_loader.write_runtime_env_file(
                    disk_target,
                    {"DATABASE_URL": "postgresql://secret"},
                )

            out = secrets_loader.write_runtime_env_file(
                disk_target,
                {"DATABASE_URL": "postgresql://secret", "GROQ_API_KEY": "gsk_123"},
                allow_non_tmpfs=True,
            )
            mode = stat.S_IMODE(out.stat().st_mode)
            self.assertEqual(mode, 0o600)
            content = out.read_text(encoding="utf-8")
            self.assertIn("DATABASE_URL=postgresql://secret\n", content)
            self.assertIn("GROQ_API_KEY=gsk_123\n", content)


class TestConfigAndDeployIntegration(unittest.TestCase):
    def setUp(self):
        secrets_loader.clear_secrets_cache()
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        secrets_loader.clear_secrets_cache()
        config.reload_secrets(force_reload=True, override=False)

    def test_config_reload_secrets_updates_module_attributes(self):
        """config.reload_secrets() reflete os parametros injetados pelo secrets_loader."""
        pages = [
            {
                "Parameters": [
                    {"Name": "/braga-saude/production/DATABASE_URL", "Value": "postgresql://ssm_user@127.0.0.1:5432/ssm_db"},
                    {"Name": "/braga-saude/production/GROQ_API_KEY", "Value": "gsk_ssm_key"},
                    {"Name": "/braga-saude/production/PUBLIC_API_BASE_URL", "Value": "https://ssm.bragasaude.com.br/"},
                    {"Name": "/braga-saude/production/SERVICE_TOKEN", "Value": "svc_ssm_token"},
                ]
            }
        ]
        mock_ssm = _make_mock_ssm_client(pages=pages)
        with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True), \
                patch("secrets_loader.get_ssm_client", return_value=mock_ssm):
            config.reload_secrets(force_reload=True, override=True)
            self.assertTrue(config.USE_AWS_SECRETS)
            self.assertEqual(config.DATABASE_URL, "postgresql://ssm_user@127.0.0.1:5432/ssm_db")
            self.assertEqual(config.MIGRATIONS_DATABASE_URL, "postgresql://ssm_user@127.0.0.1:5432/ssm_db")
            self.assertEqual(config.GROQ_API_KEY, "gsk_ssm_key")
            self.assertEqual(config.GROQ_API_KEY_CHAT, "gsk_ssm_key")
            self.assertEqual(config.PUBLIC_API_BASE_URL, "https://ssm.bragasaude.com.br")
            self.assertEqual(config.SERVICE_TOKEN, "svc_ssm_token")

    def test_deploy_and_systemd_use_in_memory_secrets_without_disk_append(self):
        """Valida que deploy.sh e braga-gateway.service usam secrets_loader e /run tmpfs."""
        repo_root = SERVER_DIR.parent.parent
        deploy_content = (repo_root / "deploy.sh").read_text(encoding="utf-8")
        service_content = (repo_root / "infra" / "systemd" / "braga-gateway.service").read_text(encoding="utf-8")

        self.assertIn("secrets_loader", deploy_content)
        self.assertIn("/run/braga-saude/gateway.env", deploy_content)
        self.assertNotIn('>> "$ENV_FILE"', deploy_content)

        self.assertIn("EnvironmentFile=-/run/braga-saude/gateway.env", service_content)
        self.assertIn("Environment=USE_AWS_SECRETS=1", service_content)
        self.assertIn("Environment=AWS_SSM_PREFIX=/braga-saude/production", service_content)

    def test_cli_main_check_and_strict_error(self):
        """Testa o entrypoint CLI main() em modo --check e falha com --strict."""
        with patch.dict(os.environ, {"USE_AWS_SECRETS": "0"}, clear=True):
            rc = secrets_loader.main(["--check"])
            self.assertEqual(rc, 0)

        mock_ssm = _make_mock_ssm_client(
            paginate_side_effect=_make_client_error("AccessDeniedException")
        )
        with patch.dict(os.environ, {"USE_AWS_SECRETS": "1"}, clear=True), \
                patch("secrets_loader.get_ssm_client", return_value=mock_ssm):
            rc_err = secrets_loader.main(["--check", "--strict"])
            self.assertEqual(rc_err, 1)


if __name__ == "__main__":
    unittest.main()
