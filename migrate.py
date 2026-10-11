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
# DSN dedicada do executor de migrations (T-06): as migrations exigem DDL e
# rodam com a role de migracao (superuser). O app conecta com DATABASE_URL,
# que e a role limitada bragasaude_app (DML apenas). Sem esta variavel o
# runner cai no DATABASE_URL e o preflight de privilegio acusa a falta de
# CREATE antes de aplicar qualquer migration.
MIGRATIONS_DATABASE_URL = os.getenv("MIGRATIONS_DATABASE_URL") or DATABASE_URL


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
    # AUD-G(M) 11/10/2026: o `continue` do stripper original NAO avancava
    # `pos`, entao os offsets deixavam de corresponder ao SQL original e o
    # inside_dollar() passava a julgar as linhas seguintes pela posicao de
    # uma linha ja removida. Consequencia medida em migrations reais:
    # 023 perdia o BEGIN do bloco DO $$ (sintaxe partida) e 040 mantinha
    # um COMMIT; de nivel zero (commit prematuro dentro da transacao).
    # O avanco de offsets acontece SEMPRE, removida a linha ou nao.
    for line in sql.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = body.strip().upper()
        is_ctrl = stripped in ("BEGIN;", "COMMIT;", "BEGIN", "COMMIT")
        # pos aponta para o 1o char desta linha no SQL original — so assim
        # inside_dollar() (que usa offsets absolutos) funciona.
        if not (is_ctrl and not inside_dollar(pos)):
            out.append(body)
        pos += len(line)
    return "\n".join(out)


def _arquivos_de_migration() -> list[Path]:
    """Ordena os .sql do diretorio, excluindo rollbacks.

    `025_app_role_rollback.sql` mora em migrations/ por convencao, mas o
    runner percorre todos os `*.sql`: sem este filtro o rollback roda logo
    depois da migration correspondente e desfaz o que ela acabou de criar
    (DROP ROLE no meio do deploy).
    """
    return sorted(
        f for f in MIGRATIONS_DIR.glob("*.sql") if not f.stem.endswith("_rollback")
    )


async def _exige_privilegio_ddl(conn) -> None:
    """Aborta com mensagem acionavel quando a role nao pode executar DDL.

    Sem este preflight a falha aparecia como InsufficientPrivilegeError no
    meio do traceback (permission denied for schema public), sem dizer qual
    variavel corrigir.
    """
    can_create = await conn.fetchval(
        "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
    )
    if can_create:
        return

    user = await conn.fetchval("SELECT current_user")
    print(f"[Error] Role '{user}' nao possui CREATE no schema 'public'.")
    print("[Error] Migrations exigem DDL: defina MIGRATIONS_DATABASE_URL no .env")
    print("[Error] com a role de migracao (superuser) e reinicie o servico.")
    print("[Error] DATABASE_URL deve continuar sendo a role limitada da aplicacao.")
    sys.exit(1)


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
    if not MIGRATIONS_DATABASE_URL:
        print("[Error] DATABASE_URL nao configurada.")
        sys.exit(1)

    conn = await asyncpg.connect(MIGRATIONS_DATABASE_URL)
    try:
        await _exige_privilegio_ddl(conn)
        applied = await get_applied_migrations(conn)
        sql_files = _arquivos_de_migration()
        print("\n=== Status das Migrations do Banco ===")
        for f in sql_files:
            v = f.stem
            status = "[APLICADA]  " if v in applied else "[PENDENTE]  "
            print(f" {status} {f.name}")
        print("======================================\n")
    finally:
        await conn.close()

async def run_migrate():
    if not MIGRATIONS_DATABASE_URL:
        print("[Error] DATABASE_URL nao configurada.")
        sys.exit(1)

    if not MIGRATIONS_DIR.exists():
        print(f"[Error] Diretorio de migrations nao encontrado: {MIGRATIONS_DIR}")
        sys.exit(1)

    conn = await asyncpg.connect(MIGRATIONS_DATABASE_URL)
    try:
        # AUD-G(M): SEM advisory lock, duas execucoes concorrentes do migrate
        # (deploy + cron, ou dois pods subindo juntos) liam a mesma lista de
        # pendentes e aplicavam a mesma migration DUAS VEZES — DDL idempotente
        # mascara a maioria, mas um ALTER ou CREATE INDEX concorrente pode
        # corromper o schema_migrations e deixar os nodes em versoes diferentes.
        # Lock por-sessao; liberado no finally.
        # Falha previa e legivel: a role conectada precisa de DDL.
        await _exige_privilegio_ddl(conn)

        LOCK_KEY = -872314509  # chave fixa, BragaSaude migrations
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        print("[Migrations] Lock consultivo obtido.")

        applied = await get_applied_migrations(conn)
        sql_files = _arquivos_de_migration()
        pending = [f for f in sql_files if f.stem not in applied]

        if not pending:
            print("[Migrations] Nenhuma migration pendente. Banco 100% atualizado.")
            return

        print(f"[Migrations] Encontradas {len(pending)} migrations pendentes.")
        for f in pending:
            version = f.stem
            print(f"[Migrations] Aplicando: {f.name}...")

            sql_content = f.read_text(encoding="utf-8-sig")
            # AUD-G(M): o stripper antigo removia TODA linha "BEGIN"/"COMMIT"
            # — incluindo as dentro de blocos DO $$ ... $$ e corpos de
            # FUNCTION (PL/pgSQL), partindo a sintaxe. Agora so remove
            # controle de transacao FORA de dollar-quotes.
            clean_sql = _strip_transaction_control(sql_content)

            # AUD-G(M) 11/10/2026 (DT-003): caminho unico e atomico.
            # Antes havia dois caminhos com comportamento diferente:
            # (a) psql aplicava o arquivo NA INTEGRA numa conexao e o
            #     registro da versao ia por OUTRA conexao (asyncpg) — janela
            #     entre DDL aplicado e ledger gravado;
            # (b) com psql ausente, o fallback asyncpg usava o stripper e
            #     seguia adiante APOS erro de SQL, reexecutando a migration.
            # Agora DDL e schema_migrations rodam na MESMA transacao da
            # MESMA conexao: ou tudo aplica, ou nada (rollback).
            # Seguro: nenhuma migration usa CONCURRENTLY/VACUUM/RESTART
            # (auditado — esses comandos nao rodam dentro de transacao).
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '10s';")
                    await conn.execute("SET LOCAL statement_timeout = '60s';")
                    await conn.execute(clean_sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING;",
                        version
                    )
            except Exception as exc:
                print(f"[Migrations] FALHA em {f.name}: rollback da transacao, nada foi aplicado.")
                print(f"[Migrations] Motivo: {type(exc).__name__}: {exc}")
                raise

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
