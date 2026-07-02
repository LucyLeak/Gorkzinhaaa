"""Fix the tts_solicitacoes CHECK constraint to include 'reproduzido' status."""
import asyncio
import sys
sys.path.insert(0, ".")

from youtube_bot.db.pool import Database
from youtube_bot.config import load_settings


async def main():
    settings = load_settings()
    db = Database(settings.database_url)
    await db.connect()

    await db.execute(
        "ALTER TABLE tts_solicitacoes DROP CONSTRAINT IF EXISTS tts_solicitacoes_status_check"
    )
    await db.execute(
        "ALTER TABLE tts_solicitacoes ADD CONSTRAINT tts_solicitacoes_status_check "
        "CHECK (status IN ('pendente', 'processando', 'concluido', 'erro', 'reproduzido'))"
    )
    print("Constraint atualizada com sucesso!")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
