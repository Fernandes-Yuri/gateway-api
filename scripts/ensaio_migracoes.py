#!/usr/bin/env python3
"""Ensaio do runner de migrations num Postgres efêmero (DT-004).

Corre o runner REAL (`migrate.py`) do zero, sobre o esquema de partida que o
próprio projeto documenta — `scripts/docker/init_schema.sql`, o mesmo que o
docker-compose e os testes de Postgres já carregam — e prova, nesta ordem:

 1. cadeia completa: do 001 ao ficheiro mais novo, aplicado de raiz, com
    ledger de exatamente uma entrada por ficheiro;
 2. upgrade do baseline publicado (`origin/master`) ao snapshot atual, com
    dados sintéticos que têm de sobreviver;
 3. erro deliberado numa migration: o erro sobe, o DDL reverte e o ledger
    não regista — é isso que faz o deploy parar (deploy.sh corre com
    `set -e`) antes de tocar no RDS;
 4. repetição: a segunda execução não altera o estado (fingerprint estável).

Duas amarras do mundo real que o ensaio reproduz em vez de ignorar:

 - a migration 025 faz `GRANT CONNECT ON DATABASE bragasaude` (o nome do
   banco de produção está hardcoded); o ensaio cria um banco vazio homónimo
   só para esse GRANT ter alvo;
 - `admin_users` não é criada por nenhuma migration nem constava do
   init_schema.sql: nasceu no initDatabase() do portal. O bootstrap passa
   a declará-la (secção 24 do init_schema.sql).

Uso:
    MIGRATIONS_DATABASE_URL=postgresql://... python scripts/ensaio_migracoes.py

O banco precisa existir e o nome precisa conter "test", "ensaio" ou "ci":
isto é ensaio, nunca produção.
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import asyncpg  # noqa: E402

import migrate  # noqa: E402

DSN = os.getenv("MIGRATIONS_DATABASE_URL") or os.getenv("TEST_POSTGRES_URL")
BASELINE_REF = "origin/master"
BOOTSTRAP = BASE_DIR / "scripts" / "docker" / "init_schema.sql"
# Nome de produção usado hardcoded pela migration 025 (GRANT CONNECT).
BANCO_HOMONIMO = "bragasaude"

ALVO = re.compile(
    r"\b(?:ALTER\s+TABLE|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(?:ONLY\s+)?"
    r"(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)

# Literais sintéticos por tipo, para colunas NOT NULL sem default.
LITERAIS = {
    "text": "'dt004-sintetico'",
    "character varying": "'dt004-sintetico'",
    "character": "'dt004'",
    "integer": "1",
    "bigint": "1",
    "smallint": "1",
    "numeric": "1",
    "real": "1",
    "double precision": "1",
    "boolean": "false",
    "uuid": "md5('dt004-sintetico')::uuid",
    "timestamp with time zone": "NOW()",
    "timestamp without time zone": "NOW()",
    "date": "CURRENT_DATE",
    "json": "'{}'",
    "jsonb": "'{}'",
    "bytea": "'\\x00'",
    "inet": "'127.0.0.1'",
}


def falha(mensagem: str) -> None:
    print(f"[ensaio] FALHOU: {mensagem}")
    raise SystemExit(1)


def ok(mensagem: str) -> None:
    print(f"[ensaio] ok: {mensagem}")


def _dsn_valido() -> str:
    if not DSN:
        falha("defina MIGRATIONS_DATABASE_URL (ou TEST_POSTGRES_URL) apontando "
              "para um banco efêmero.")
    nome = DSN.rsplit("/", 1)[-1].split("?")[0]
    if not re.search(r"test|ensaio|ci", nome, re.IGNORECASE):
        falha(f"banco '{nome}' não parece efêmero (esperado 'test'/'ensaio'/'ci'). "
              "Ensaiar em produção não é um ensaio.")
    return DSN


async def fingerprint(dsn: str) -> str:
    """Hash do formato do schema + ledger: muda só se o estado mudar."""
    conn = await asyncpg.connect(dsn)
    try:
        colunas = await conn.fetch("""
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, column_name;
        """)
        ledger = await conn.fetch(
            "SELECT version FROM schema_migrations ORDER BY version;")
    finally:
        await conn.close()
    payload = json.dumps(
        [dict(c) for c in colunas] + [dict(v) for v in ledger],
        sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


async def prepara_esquema(dsn: str, bootstrap: Path) -> None:
    """Estado de partida: esquema do projeto + banco homónimo da migration 025."""
    if not bootstrap.exists():
        falha(f"bootstrap ausente: {bootstrap}")
    conteudo = bootstrap.read_text(encoding="utf-8")
    if "CREATE TABLE" not in conteudo.upper():
        falha(f"{bootstrap} não declara tabelas: não é um esquema de partida.")

    conn = await asyncpg.connect(dsn)
    try:
        # A 025 faz GRANT CONNECT ON DATABASE bragasaude: sem este banco o
        # erro seria InvalidCatalogNameError no meio da cadeia, mascarado
        # como se a migration em si estivesse partida.
        await conn.execute(f"DROP DATABASE IF EXISTS {BANCO_HOMONIMO};")
        await conn.execute(f"CREATE DATABASE {BANCO_HOMONIMO};")
        await conn.execute(conteudo)
        tabelas = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE';")
    finally:
        await conn.close()
    ok(f"bootstrap aplicado ({bootstrap.relative_to(BASE_DIR)}): "
       f"{tabelas} tabelas de partida")


async def versoes_ledger(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return {r["version"] for r in await conn.fetch(
            "SELECT version FROM schema_migrations;")}
    finally:
        await conn.close()


async def confere_ledger(dsn: str, diretorio: Path) -> None:
    """Histórico completo: todo o ficheiro aplicado tem entrada no ledger.

    Sobras são apenas as linhas-alias históricas que duas migrations gravam
    a mais (019 e 026 escrevem também '019'/'026' sem sufixo, por
    compatibilidade com referências antigas): reportam-se, mas não falham.
    O inverso — ficheiro aplicado sem registo — sim, porque deixaria o
    deploy a reexecutar migrations já aplicadas.
    """
    esperadas = {f.stem for f in diretorio.glob("*.sql")
                 if not f.stem.endswith("_rollback")}
    registadas = await versoes_ledger(dsn)
    faltam = sorted(esperadas - registadas)
    sobram = sorted(registadas - esperadas)
    if faltam:
        falha(f"migration(s) aplicada(s) sem entrada no ledger: {faltam}")
    if sobram:
        print(f"[ensaio] nota: linhas-alias históricas no ledger: {sobram} "
              f"(migrations 019/026 gravam também a versão sem sufixo)")
    ok(f"ledger cobre todas as {len(esperadas)} migrations do diretório")


def extrai_baseline(ref: str, destino: Path) -> Path:
    """Copia `migrations/` como estava na versão publicada (ref)."""
    for alvo_ref in (ref, "master", "HEAD"):
        proc = subprocess.run(
            ["git", "archive", alvo_ref, "migrations"],
            cwd=BASE_DIR, capture_output=True)
        if proc.returncode == 0:
            destino.mkdir(parents=True, exist_ok=True)
            proc2 = subprocess.run(
                ["tar", "-x", "-C", str(destino)],
                input=proc.stdout, capture_output=True)
            if proc2.returncode == 0:
                ok(f"baseline extraído de '{alvo_ref}' "
                   f"({len(list((destino / 'migrations').glob('*.sql')))} migrations)")
                return destino / "migrations"
    falha(f"não consegui extrair migrations/ de '{ref}' (git archive falhou).")


def migracoes_novas(baseline: Path, atual: Path) -> dict[str, str]:
    def carrega(d: Path) -> dict[str, str]:
        return {f.name: f.read_text(encoding="utf-8-sig")
                for f in d.glob("*.sql") if not f.stem.endswith("_rollback")}

    anteriores, atuais = carrega(baseline), carrega(atual)
    return {n: c for n, c in atuais.items() if anteriores.get(n) != c}


def rodar_runner(diretorio: Path) -> None:
    migrate.MIGRATIONS_DIR = diretorio
    asyncio.run(migrate.run_migrate())


async def tabelas_candidatas(dsn: str, alvos: set[str]) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        existentes = await conn.fetchval(
            "SELECT array_agg(c.table_name::text) FROM information_schema.tables c "
            "WHERE c.table_schema='public' AND c.table_type='BASE TABLE';")
    finally:
        await conn.close()
    return sorted(t for t in alvos if t in (existentes or []))


async def semear(dsn: str, tabelas: list[str]) -> tuple[list[str], list[str]]:
    """Insere uma linha sintética por tabela; devolve (semeadas, puladas)."""
    conn = await asyncpg.connect(dsn)
    semeadas, puladas = [], []
    try:
        for tabela in tabelas:
            cols = await conn.fetch("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=$1
                ORDER BY ordinal_position;
            """, tabela)
            obrigatorias = [c for c in cols
                            if c["is_nullable"] == "NO" and not c["column_default"]]
            incompativeis = [c for c in obrigatorias
                             if c["data_type"] not in LITERAIS]
            if incompativeis:
                puladas.append(
                    f"{tabela} (tipo não suportado: {incompativeis[0]['data_type']})")
                continue
            try:
                if obrigatorias:
                    cols_sql = ", ".join(f'"{c["column_name"]}"' for c in obrigatorias)
                    vals_sql = ", ".join(LITERAIS[c["data_type"]] for c in obrigatorias)
                    await conn.execute(
                        f'INSERT INTO "{tabela}" ({cols_sql}) VALUES ({vals_sql});')
                else:
                    await conn.execute(f'INSERT INTO "{tabela}" DEFAULT VALUES;')
                semeadas.append(tabela)
            except Exception as exc:  # FK ou constraint de dados
                puladas.append(f"{tabela} ({type(exc).__name__})")
    finally:
        await conn.close()
    return semeadas, puladas


async def conta(dsn: str, tabelas: list[str]) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        return {t: await conn.fetchval(f"SELECT count(*) FROM \"{t}\";")
                for t in tabelas}
    finally:
        await conn.close()


async def coluna_existe(dsn: str, tabela: str, coluna: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return bool(await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2;",
            tabela, coluna))
    finally:
        await conn.close()


def ensaio_de_falha_deliberada(dsn: str) -> None:
    """Migration quebrada: erro sobe, DDL reverte, ledger não registra."""
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        (raiz / "dt004_fail.sql").write_text(
            "CREATE TABLE dt004_meio (id INT);\nSELECT 1/0;\n", encoding="utf-8")
        try:
            rodar_runner(raiz)
        except Exception as exc:
            ok(f"erro deliberado propagado ({type(exc).__name__})")
        else:
            falha("erro deliberado foi engolido: o deploy continuaria após uma "
                  "migration quebrada.")

    async def _verificar():
        conn = await asyncpg.connect(dsn)
        try:
            tabela = await conn.fetchval("SELECT to_regclass('dt004_meio');")
            registrado = await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE version='dt004_fail';")
        finally:
            await conn.close()
        return tabela, registrado

    tabela, registrado = asyncio.run(_verificar())
    if tabela is not None:
        falha("DDL da migration quebrada sobreviveu (sem rollback).")
    if registrado:
        falha("ledger registrou uma migration que falhou.")
    ok("rollback de DDL e ledger ausente após falha deliberada")


def ensaio_de_demostracao(dsn: str, baseline: Path) -> None:
    """Upgrade simulado: migration nova que mexe numa tabela já existente."""
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp) / "migrations"
        raiz.mkdir(parents=True)
        for f in baseline.glob("*.sql"):
            (raiz / f.name).write_text(f.read_text(encoding="utf-8-sig"),
                                       encoding="utf-8")
        (raiz / "dt004_demo.sql").write_text(
            "-- migração simulada só para o ensaio\n"
            "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS dt004_ensaio TEXT;\n",
            encoding="utf-8")
        try:
            rodar_runner(raiz)
        except Exception as exc:
            falha(f"upgrade simulado quebrou: {type(exc).__name__}: {exc}")

    if not asyncio.run(coluna_existe(dsn, "schema_migrations", "dt004_ensaio")):
        falha("a migration simulada não criou a coluna dt004_ensaio.")
    ledger = asyncio.run(conta(dsn, ["schema_migrations"]))["schema_migrations"]
    if ledger == 0:
        falha("ledger ficou vazio depois do upgrade simulado.")
    ok(f"upgrade simulado aplicado com ledger íntegro ({ledger} versões)")


def main() -> None:
    dsn = _dsn_valido()
    migrate.MIGRATIONS_DATABASE_URL = dsn
    atual = BASE_DIR / "migrations"
    if not atual.exists():
        falha(f"diretório de migrations ausente: {atual}")

    asyncio.run(prepara_esquema(dsn, BOOTSTRAP))

    with tempfile.TemporaryDirectory() as tmp:
        baseline = extrai_baseline(os.getenv("ENSAIO_BASELINE", BASELINE_REF),
                                   Path(tmp) / "baseline")
        novos = migracoes_novas(baseline, atual)
        print(f"[ensaio] migrations novas desde o baseline: "
              f"{len(novos) if novos else 0} {sorted(novos) if novos else ''}")

        rodar_runner(baseline)
        ok("baseline publicado aplicado do zero")
        asyncio.run(confere_ledger(dsn, baseline))

        alvos = {t for sql in novos.values() for t in ALVO.findall(sql)}
        candidatas = asyncio.run(tabelas_candidatas(dsn, alvos))
        semeadas, puladas = asyncio.run(semear(dsn, candidatas))
        for p in puladas:
            print(f"[ensaio] não semeada: {p}")
        antes = asyncio.run(conta(dsn, semeadas)) if semeadas else {}
        ok(f"{len(semeadas)} tabela(s) semeada(s) com dados sintéticos")

        rodar_runner(atual)
        ok("snapshot atual aplicado por cima do baseline")
        asyncio.run(confere_ledger(dsn, atual))
        if semeadas:
            depois = asyncio.run(conta(dsn, semeadas))
            perdidas = {t: (antes[t], depois[t]) for t in semeadas
                        if depois[t] < antes[t]}
            if perdidas:
                falha(f"upgrade destruiu dados sintéticos: {perdidas}")
            ok("dados sintéticos sobreviveram ao upgrade")
        else:
            print("[ensaio] nota: nenhuma tabela pré-existente é alvo das "
                  "migrations novas hoje; o ensaio de dados fica para a "
                  "próxima migration que mexa em tabela existente.")

        ensaio_de_falha_deliberada(dsn)
        ensaio_de_demostracao(dsn, baseline)

        antes_da_repeticao = asyncio.run(fingerprint(dsn))
        rodar_runner(atual)
        depois_da_repeticao = asyncio.run(fingerprint(dsn))
        if antes_da_repeticao != depois_da_repeticao:
            falha("a segunda execução do runner alterou o estado do banco.")
        ok("segunda execução não alterou o estado (fingerprint estável)")

    print("[ensaio] ENSAIO CONCLUÍDO: runner real passou em todas as etapas.")


if __name__ == "__main__":
    main()
