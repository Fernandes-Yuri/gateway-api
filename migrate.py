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
            
            import shutil, subprocess
            applied_successfully = False
            if shutil.which("psql"):
                # AUD-G(M): antes a senha do RDS vinha na linha de comando do
                # psql (argv) — visivel para QUALQUER usuario do host em
                # `ps aux` / `ps -ef`. Agora ela viaja so no ambiente (PGPASSWORD),
                # e o DSN passado ao psql nao carrega credencial.
                dsn_safe, psql_env = _psql_dsn_without_password()
                cmd = ["psql", dsn_safe, "-v", "ON_ERROR_STOP=1", "-f", str(f)]
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
                clean_lines = []
                for line in sql_content.splitlines():
                    stripped = line.strip().upper()
                    if stripped in ("BEGIN;", "COMMIT;", "BEGIN", "COMMIT"):
                        continue
                    clean_lines.append(line)
                clean_sql = "\n".join(clean_lines)
                
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
        await conn.close()

if __name__ == "__main__":
    if "--status" in sys.argv:
        asyncio.run(run_status())
    else:
        asyncio.run(run_migrate())
