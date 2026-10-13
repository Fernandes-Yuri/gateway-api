"""Publicação OTA: arquivos imutáveis e ponteiro ativo transacional no banco."""
import asyncio
import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RELEASE_LOCK = 723194602
SIGNING_CERT_SHA256 = 'a3a1c2f78dabb70decb230861005ab64c73adaa3640937c2a9f21ba6d5b82069'


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    package_name: Literal['br.com.bragasaude']
    variant: Literal['release']
    debuggable: Literal[False]
    version_name: str = Field(pattern=r'^\d+\.\d+\.\d+(?:[-.][a-zA-Z0-9]+)*$')
    version_code: int = Field(gt=0, strict=True)
    min_android_sdk: int = Field(ge=26, strict=True)
    file_size_bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    certificate_sha256: Literal[SIGNING_CERT_SHA256]
    source_sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    run_id: int = Field(gt=0, strict=True)

    @property
    def filename(self):
        return f'BragaSaude-v{self.version_name}-build{self.version_code}-{self.sha256[:16]}.apk'


def verify_file(root, manifest):
    root = Path(root).resolve()
    path = root / manifest.filename
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        raise ValueError('APK imutável ausente ou caminho inválido')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        if os.fstat(stream.fileno()).st_size != manifest.file_size_bytes or digest.hexdigest() != manifest.sha256:
            raise ValueError('APK diverge do tamanho/hash aprovado')
        os.fsync(stream.fileno())
    path.chmod(0o444)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


async def publish_release(pool, root, manifest: ReleaseManifest, public_base_url):
    if pool is None:
        raise RuntimeError('Banco indisponível para publicação')
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1)', RELEASE_LOCK)
            # Retenção usa o mesmo lock: nenhum arquivo pode sumir antes do commit.
            await asyncio.to_thread(verify_file, root, manifest)
            existing = await conn.fetchrow(
                'SELECT version_name, sha256_hash, filename, is_active FROM app_releases WHERE version_code=$1 FOR UPDATE',
                manifest.version_code)
            if existing and (existing['sha256_hash'].lower() != manifest.sha256 or
                             existing['version_name'].removeprefix('v') != manifest.version_name):
                raise ValueError('Version code já publicado com outros bytes/metadados')
            newest = await conn.fetchval('SELECT max(version_code) FROM app_releases')
            if newest and manifest.version_code < newest:
                raise ValueError('Promoção não pode reativar versão anterior')
            if existing and existing['is_active'] and existing['filename'] == manifest.filename:
                return {'status': 'already_active', 'version_code': manifest.version_code, 'filename': manifest.filename}
            was_active = bool(existing and existing['is_active'])
            await conn.execute('UPDATE app_releases SET is_active=FALSE WHERE is_active=TRUE')
            await conn.execute(
                """INSERT INTO app_releases(version_name,version_code,release_type,filename,file_size_bytes,
                   sha256_hash,download_url,changelog,min_android_sdk,is_active)
                   VALUES($1,$2,'release',$3,$4,$5,$6,$7,$8,TRUE)
                   ON CONFLICT(version_code) DO UPDATE SET filename=EXCLUDED.filename,
                   file_size_bytes=EXCLUDED.file_size_bytes,sha256_hash=EXCLUDED.sha256_hash,
                   download_url=EXCLUDED.download_url,changelog=EXCLUDED.changelog,is_active=TRUE""",
                'v' + manifest.version_name, manifest.version_code, manifest.filename,
                manifest.file_size_bytes, manifest.sha256.upper(),
                f'{public_base_url}/api/app/download?version_code={manifest.version_code}',
                f'Release {manifest.version_name}, build {manifest.version_code}; commit {manifest.source_sha}; run {manifest.run_id}.',
                manifest.min_android_sdk)
    return {'status': 'already_active' if was_active else 'published',
            'version_code': manifest.version_code, 'filename': manifest.filename}
