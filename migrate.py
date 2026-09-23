#!/usr/bin/env python3
"""Braga Saude - Database Migration Runner
Aplica migrations SQL de forma segura, transacional e idempotente no PostgreSQL (RDS).
"""
import os
import sys
import asyncio
from pathlib import Path
import asyncpg
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BASE_DIR / "migrations"

# Carrega variáveis de ambiente
for env_candidate in [
    BASE_DIR / ".env",
    BASE_DIR.parent / ".env",
    Path("/home/ec2-user/braga-gateway/.env"),
    BASE_DIR / "app" / ".env",
]:
    if env_candidate.exists():
        load_dotenv(env_candidate)

DATABASE_URL = os.getenv("DATABASE_URL")


def _strip_transaction_control(sql: str) -> str:
    """Remove BEGIN/COMMIT de nivel-zero, preservando PL/pgSQL.

    Linhas "BEGIN"/"COMMIT" soltas no nivel zero do SQL seriam aninhadas
    dentro da transacao asyncpg (warning + COMMIT prematuro). Mas o mesmo
    token dentro de DO $$ ... $$ ou corpo de FUNCTION e codigo PL/pgSQL —
    remover parte a sintaxe. Por isso so tocamos o que esta FORA de
    dollar-quotes.
    """
    import re
    # Captura spans $tag$ ... $tag$ (tag vazia ou nomeada), pares completos.
    token = re.compile(r"\$[A-Za-z_0-9]*\$")
    spans = []  # (start, end) de cada par aberto+fechado
    open_tags = []  # pilha de (start, tag)
    for m in token.finditer(sql):
        tag = m.group(0)
        if open_tags and open_tags[-1][1] == tag:
            s, _ = open_tags.pop()
            spans.append((s, m.end()))
        else:
            open_tags.append((m.start(), tag))
    # Se ficou tag aberta (SQL malformado), nao remove nada — fail-safe.
    if open_tags:
        return sql

    def inside_dollar(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    out = []
    pos = 0
    for line in sql.splitlines():
        stripped = line.strip().upper()
        is_ctrl = stripped in ("BEGIN;", "COMMIT;", "BEGIN", "COMMIT")
        # pos aponta para o 1o char desta linha no SQL original — so assim
        # inside_dollar() (que usa offsets absolutos) funciona.
        if is_ctrl and not inside_dollar(pos):
            continue
        out.append(line)
        pos += len(line) + 1  # +1 pelo \n que splitlines descartou
    return "\n".join(out)


async def get_applied_migrations(conn) -> set:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version;")
    return {r["version"] for r in rows}

async def run_status():
    if not DATABASE_URL:
        print("[Error] DATABASE_URL nao configurada.")
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        applied = await get_applied_migrations(conn)
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        print("\n=== Status das Migrations do Banco ===")
        for f in sql_files:
            v = f.stem
            status = "[APLICADA]  " if v in applied else "[PENDENTE]  "
            print(f" {status} {f.name}")
        print("======================================\n")
    finally:
        await conn.close()

def _psql_dsn_without_password() -> tuple[str, dict]:
    """AUD-G(M): devolve (dsn sem senha, env com PGPASSWORD).

    Antes a URL completa — incluindo a senha do RDS — era passada como
    argumento do psql, expondo a credencial a qualquer usuario do host via
    `ps aux` (ficou visivel no processo da pipeline de deploy).
    """
    import urllib.parse

    env = dict(os.environ)
    parsed = urllib.parse.urlparse(DATABASE_URL)
    if parsed.password:
        env["PGPASSWORD"] = urllib.parse.unquote(parsed.password)
    # Reconstrói o DSN SEM a senha: scheme://user@host:port/path?params
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    if parsed.username:
        netloc = f"{urllib.parse.quote(parsed.username, safe='')}@{netloc}"
    dsn_safe = urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return dsn_safe, env


async def run_migrate():
    if not DATABASE_URL:
        print("[Error] DATABASE_URL nao configurada.")
        sys.exit(1)

    if not MIGRATIONS_DIR.exists():
        print(f"[Error] Diretorio de migrations nao encontrado: {MIGRATIONS_DIR}")
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # AUD-G(M): SEM advisory lock, duas execucoes concorrentes do migrate
        # (deploy + cron, ou dois pods subindo juntos) liam a mesma lista de
        # pendentes e aplicavam a mesma migration DUAS VEZES — DDL idempotente
        # mascara a maioria, mas um ALTER ou CREATE INDEX concorrente pode
        # corromper o schema_migrations e deixar os nodes em versoes diferentes.
        # Lock por-sessao; liberado no finally.
        LOCK_KEY = -872314509  # chave fixa, BragaSaude migrations
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        print("[Migrations] Lock consultivo obtido.")

        applied = await get_applied_migrations(conn)
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [f for f in sql_files if f.stem not in applied]

        if not pending:
            print("[Migrations] Nenhuma migration pendente. Banco 100% atualizado.")
            return

        print(f"[Migrations] Encontradas {len(pending)} migrations pendentes.")
        for f in pending:
            version = f.stem
            print(f"[Migrations] Aplicando: {f.name}...")
            
            import shutil
            import subprocess
            applied_successfully = False
            if shutil.which("psql"):
                # AUD-G(M): antes a senha do RDS vinha na linha de comando do
                # psql (argv) — visivel para QUALQUER usuario do host em
                # `ps aux` / `ps -ef`. Agora ela viaja so no ambiente (PGPASSWORD),
                # e o DSN passado ao psql nao carrega credencial.
                dsn_safe, psql_env = _psql_dsn_without_password()
                # AUD-G(M): --single-transaction envolve o arquivo inteiro num
                # BEGIN/COMMIT — antes cada statement rodava em autocommit, e
                # uma migration de 4 statements que falhasse no #3 deixava #1
                # e #2 aplicados e o schema_migrations sem registro da versao
                # (estado parcial, dificil de diagnosticar). Com ON_ERROR_STOP
                # + single-transaction, ou o arquivo inteiro aplica ou nada.
                # Seguro: nenhuma migration usa CONCURRENTLY/VACUUM/REINDEX
                # (auditado — esses comandos nao rodam dentro de transacao).
                cmd = ["psql", dsn_safe, "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", str(f)]
                res = subprocess.run(cmd, capture_output=True, text=True, env=psql_env)
                if res.returncode == 0:
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING;",
                        version
                    )
                    applied_successfully = True
                else:
                    print(f"[Migrations] psql notice/stderr: {res.stderr.strip()}")
            
            if not applied_successfully:
                sql_content = f.read_text(encoding="utf-8-sig")
                # AUD-G(M): o stripper antigo removia TODA linha "BEGIN"/"COMMIT"
                # — incluindo as dentro de blocos DO $$ ... $$ e corpos de
                # FUNCTION (PL/pgSQL), partindo a sintaxe. Agora so remove
                # controle de transacao FORA de dollar-quotes.
                clean_sql = _strip_transaction_control(sql_content)
                
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '10s';")
                    await conn.execute("SET LOCAL statement_timeout = '60s';")
                    await conn.execute(clean_sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING;",
                        version
                    )

            print(f"[Migrations] OK: {f.name} aplicada com sucesso!")


        print("[Migrations] Todas as migrations foram aplicadas com sucesso.")
    finally:
        # AUD-G(M): libera o lock consultivo antes de fechar a conexao.
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", -872314509)
        except Exception:
            pass  # conexao ja morta; o lock libera sozinho ao fechar
        await conn.close()

if __name__ == "__main__":
    if "--status" in sys.argv:
        asyncio.run(run_status())
    else:
        asyncio.run(run_migrate())
