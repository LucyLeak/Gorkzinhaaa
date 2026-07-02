from __future__ import annotations

import logging

from youtube_bot.db import models
from youtube_bot.db.pool import Database

logger = logging.getLogger(__name__)


async def cleanup_on_startup(db: Database, retention_days: int) -> dict[str, int]:
    """Executa a limpeza de dados antigos uma unica vez na inicializacao do bot.
    Retorna a contagem de registros deletados por tabela."""
    logger.info("Limpando dados com mais de %s dias (execucao unica de startup)...", retention_days)
    deleted = await models.cleanup_old_data(db, retention_days)
    total = sum(deleted.values())
    if total > 0:
        logger.info(
            "Limpeza concluida: %s registros removidos (%s).",
            total,
            ", ".join(f"{k}={v}" for k, v in deleted.items() if v > 0),
        )
    else:
        logger.info("Limpeza concluida: nenhum registro para remover.")
    return deleted
