"""Gera um TTS de teste e faz upload para Catbox usando as funções do projeto."""
import asyncio
import sys
sys.path.insert(0, ".")

from youtube_bot.config import load_settings
from youtube_bot.db.pool import Database
from youtube_bot.fun import tts


async def main():
    settings = load_settings()
    db = Database(settings.database_url)
    await db.connect()

    print("Gerando TTS de teste...")
    file_path = await tts.generate_tts("Teste Catbox upload automático", settings, db, 1)
    print("Arquivo gerado:", file_path)

    print("Fazendo upload para Catbox...")
    url = await tts._upload_to_catbox(file_path)
    print("Resultado do upload:", url)

    await db.close()


if __name__ == '__main__':
    asyncio.run(main())
