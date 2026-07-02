from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self.database_url:
            raise RuntimeError("NEON_DATABASE_URL nao foi configurada.")
        self.pool = await asyncpg.create_pool(dsn=self.database_url, min_size=1, max_size=10)
        logger.info("Pool de conexoes PostgreSQL inicializado.")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            logger.info("Pool de conexoes PostgreSQL encerrado.")

    async def execute(self, query: str, *args: Any) -> str:
        if not self.pool:
            raise RuntimeError("Banco de dados nao conectado.")
        return await self.pool.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> Sequence[asyncpg.Record]:
        if not self.pool:
            raise RuntimeError("Banco de dados nao conectado.")
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        if not self.pool:
            raise RuntimeError("Banco de dados nao conectado.")
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        if not self.pool:
            raise RuntimeError("Banco de dados nao conectado.")
        return await self.pool.fetchval(query, *args)
