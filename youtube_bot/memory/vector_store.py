from __future__ import annotations

import logging

from youtube_bot.db.pool import Database

logger = logging.getLogger(__name__)


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


class VectorMemoryStore:
    def __init__(
        self,
        db: Database,
        openai_client: object | None,
        embedding_model: str,
        embedding_dimensions: int | None = None,
    ) -> None:
        self.db = db
        self.openai_client = openai_client
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions

    @property
    def can_embed(self) -> bool:
        return self.openai_client is not None

    async def embed_text(self, text: str) -> list[float]:
        if self.openai_client is None:
            raise RuntimeError("OPENAI_API_KEY nao configurada para gerar embeddings.")
        payload = {"model": self.embedding_model, "input": text}
        if self.embedding_dimensions:
            payload["dimensions"] = self.embedding_dimensions
        response = await self.openai_client.embeddings.create(**payload)
        return list(response.data[0].embedding)

    async def store_memory(
        self,
        user_id: int,
        text: str,
        memory_type: str = "episodio",
        previous_version_id: int | None = None,
    ) -> int | None:
        if not self.can_embed:
            logger.debug("Memoria ignorada porque embeddings nao estao configurados.")
            return None

        embedding = _vector_literal(await self.embed_text(text))
        return await self.db.fetchval(
            """
            INSERT INTO memorias_semanticas (
                usuario_id, embedding, texto_resumido, tipo, versao_anterior_id
            )
            VALUES ($1, $2::vector, $3, $4, $5)
            RETURNING id
            """,
            user_id,
            embedding,
            text[:1000],
            memory_type,
            previous_version_id,
        )

    async def retrieve_similar_memories(
        self,
        user_id: int,
        query_text: str,
        top_k: int = 5,
    ) -> list[str]:
        if not self.can_embed:
            return []

        query_embedding = _vector_literal(await self.embed_text(query_text))
        rows = await self.db.fetch(
            """
            SELECT texto_resumido
            FROM memorias_semanticas
            WHERE usuario_id = $1
            ORDER BY embedding <=> $2::vector
            LIMIT $3
            """,
            user_id,
            query_embedding,
            top_k,
        )
        return [str(row["texto_resumido"]) for row in rows]
