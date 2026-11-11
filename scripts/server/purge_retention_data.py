"""Rotina de expurgo físico e retenção de dados LGPD (Mensagens 24h e Telemetria 7d).

Conforme documentado na Política de Privacidade do Braga Saúde e no Cérebro:
1. Mensagens familiares (tabela family_messages): expiram após 24 horas e devem ser
   fisicamente eliminadas em lotes utilizando o índice idx_family_messages_purge.
2. Telemetria de segurança (tabela security_request_events): expurgo de eventos após 7 dias.
"""
import asyncio
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("braga.retention_purge")


async def purge_family_messages(conn: Any, batch_size: int = 500, max_batches: int = 20) -> int:
    """Expurga fisicamente mensagens expiradas (>24h) ou com tombstone antigo em lotes atômicos."""
    total_deleted = 0
    query = """
        DELETE FROM family_messages
        WHERE id IN (
            SELECT id FROM family_messages
            WHERE expires_at < NOW()
               OR (deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '1 hour')
            ORDER BY expires_at ASC
            LIMIT $1
        )
    """
    for _ in range(max_batches):
        result = await conn.execute(query, batch_size)
        # asyncpg execute retorna string "DELETE <count>"
        deleted = 0
        if result and result.startswith("DELETE "):
            try:
                deleted = int(result.split()[1])
            except (IndexError, ValueError):
                deleted = 0
        total_deleted += deleted
        if deleted < batch_size:
            break
    logger.info("[purge] Mensagens familiares expurgadas: %s", total_deleted)
    return total_deleted


async def purge_security_telemetry(conn: Any, batch_size: int = 1000, max_batches: int = 10) -> int:
    """Expurga registros de telemetria de requisições com mais de 7 dias."""
    total_deleted = 0
    query = """
        DELETE FROM security_request_events
        WHERE id IN (
            SELECT id FROM security_request_events
            WHERE created_at < NOW() - INTERVAL '7 days'
            ORDER BY created_at ASC
            LIMIT $1
        )
    """
    for _ in range(max_batches):
        result = await conn.execute(query, batch_size)
        deleted = 0
        if result and result.startswith("DELETE "):
            try:
                deleted = int(result.split()[1])
            except (IndexError, ValueError):
                deleted = 0
        total_deleted += deleted
        if deleted < batch_size:
            break
    logger.info("[purge] Eventos de telemetria expurgados: %s", total_deleted)
    return total_deleted


async def run_retention_purge(conn: Any) -> dict[str, int]:
    """Executa a rotina completa de expurgo sobre uma conexão existente."""
    messages_deleted = await purge_family_messages(conn)
    telemetry_deleted = await purge_security_telemetry(conn)
    return {
        "family_messages_deleted": messages_deleted,
        "security_telemetry_deleted": telemetry_deleted,
    }


async def main():
    """Ponto de entrada para execução via cron ou systemd timer."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("TEST_POSTGRES_URL")
    if not db_url:
        logger.error("[purge] DATABASE_URL não configurada. Encerrando.")
        sys.exit(1)

    import asyncpg

    logger.info("[purge] Iniciando rotina de expurgo LGPD de dados expirados...")
    try:
        conn = await asyncpg.connect(db_url, timeout=10)
        try:
            summary = await run_retention_purge(conn)
            logger.info("[purge] Concluído com sucesso: %s", summary)
        finally:
            await conn.close()
    except Exception as exc:
        logger.error("[purge] Erro durante rotina de expurgo: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
