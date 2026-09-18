"""Retencao de releases de APK (D52).

Cada APK de release pesa ~36MB. Sem regra, a pasta `releases/` da EC2
cresce sem par (100 releases = 3,6GB) e pode lotar o disco.

Regra: manter apenas os K APKs mais recentes (K=3 por padrao), apagando
os mais antigos — tanto os arquivos no disco quanto as linhas no banco.

Seguranca (fail-closed):
  * nunca apaga o release ATIVO (is_active=true) nem o arquivo atualmente
    servido (BragaSaude.apk / latest.apk), mesmo que sejam antigos;
  * sem pool ou sem arquivos, nao apaga nada;
  * so age sobre arquivos .apk dentro de RELEASES_DIR.
"""
import os
from datetime import datetime, timezone

KEEP_RELEASES = 3

# Nomes reservados do APK servido — protegidos contra qualquer exclusao.
PROTECTED_NAMES = {"bragasaude.apk", "latest.apk"}


def _listar_apks(releases_dir: str) -> list:
    """Lista os .apk do diretorio com (caminho, mtime)."""
    try:
        entradas = []
        for nome in os.listdir(releases_dir):
            if not nome.lower().endswith(".apk"):
                continue
            caminho = os.path.join(releases_dir, nome)
            if os.path.isfile(caminho) and os.path.getsize(caminho) > 0:
                entradas.append((caminho, os.path.getmtime(caminho)))
        return entradas
    except OSError:
        return []


async def _releases_protegidos(conn) -> set:
    """Conjunto de nomes que nunca devem ser apagados (ativo + servido)."""
    protegidos = set(PROTECTED_NAMES)
    try:
        nome_ativo = await conn.fetchval(
            "SELECT filename FROM app_releases WHERE is_active = TRUE "
            "ORDER BY version_code DESC LIMIT 1")
        if nome_ativo:
            protegidos.add(str(nome_ativo).strip().lower())
    except Exception:
        pass
    return protegidos


async def purge_old_releases(pool, releases_dir: str, keep: int = KEEP_RELEASES) -> dict:
    """Mantem so os `keep` releases mais recentes; apaga o resto.

    Retorna {apagados: [...], mantidos: [...]} para auditoria.
    """
    resultado = {"apagados": [], "mantidos": []}
    if pool is None:
        return resultado

    apks = _listar_apks(releases_dir)
    if len(apks) <= keep:
        return resultado  # nada a fazer abaixo do limite

    async with pool.acquire() as conn:
        protegidos = await _releases_protegidos(conn)

        # Mais recentes primeiro
        apks.sort(key=lambda par: par[1], reverse=True)

        apagados_ids = []
        for i, (caminho, _mtime) in enumerate(apks):
            nome = os.path.basename(caminho).lower()
            if i < keep or nome in protegidos:
                resultado["mantidos"].append(os.path.basename(caminho))
                continue
            # Exclui o arquivo e a linha correspondente no banco
            versao = await conn.fetchval(
                "SELECT id FROM app_releases WHERE lower(filename) = $1", nome)
            try:
                os.remove(caminho)
                resultado["apagados"].append(os.path.basename(caminho))
                if versao:
                    apagados_ids.append(versao)
            except OSError:
                # Sem permicao ou ocupado: nao aborta a limpeza dos outros.
                continue

        if apagados_ids:
            placeholders = ",".join(f"${i + 1}" for i in range(len(apagados_ids)))
            await conn.execute(
                f"DELETE FROM app_releases WHERE id IN ({placeholders}) "
                f"AND is_active = FALSE", *apagados_ids)

    return resultado
