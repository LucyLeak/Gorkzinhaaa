from __future__ import annotations

import logging

from youtube_bot.db.pool import Database
from youtube_bot.memory.vector_store import VectorMemoryStore

logger = logging.getLogger(__name__)


class MemoryConsolidator:
    def __init__(
        self,
        db: Database,
        vector_store: VectorMemoryStore,
        openai_client: object | None,
        chat_model: str,
    ) -> None:
        self.db = db
        self.vector_store = vector_store
        self.openai_client = openai_client
        self.chat_model = chat_model

    async def consolidate_if_needed(self, user_id: int, every: int = 100) -> None:
        total = await self.db.fetchval(
            "SELECT count(*) FROM memorias_semanticas WHERE usuario_id = $1",
            user_id,
        )
        if not total or total % every != 0:
            return
        await self.consolidate(user_id)

    async def consolidate(self, user_id: int) -> int | None:
        rows = await self.db.fetch(
            """
            SELECT id, texto_resumido
            FROM memorias_semanticas
            WHERE usuario_id = $1 AND tipo = 'episodio'
            ORDER BY criado_em ASC
            LIMIT 50
            """,
            user_id,
        )
        if len(rows) < 10:
            return None

        texts = [str(row["texto_resumido"]) for row in rows]
        summary = await self._summarize(texts)
        previous_version_id = int(rows[-1]["id"])
        memory_id = await self.vector_store.store_memory(
            user_id=user_id,
            text=summary,
            memory_type="fato",
            previous_version_id=previous_version_id,
        )
        if memory_id:
            await self.db.execute(
                """
                DELETE FROM memorias_semanticas
                WHERE id = ANY($1::bigint[])
                """,
                [int(row["id"]) for row in rows],
            )
            logger.info("Memorias consolidadas para usuario %s.", user_id)
        return memory_id

    async def _summarize(self, texts: list[str]) -> str:
        joined = "\n".join(f"- {text}" for text in texts)
        if self.openai_client is None:
            return "Resumo consolidado: " + " ".join(texts[:5])[:900]
        response = await self.openai_client.chat.completions.create(
            model=self.chat_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Resuma memorias de conversa em fatos uteis para contexto futuro. "
                        "Seja curto, fiel e sem inventar."
                    ),
                },
                {"role": "user", "content": joined},
            ],
            temperature=0.2,
            max_tokens=180,
        )
        return (response.choices[0].message.content or "").strip()
