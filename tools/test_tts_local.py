"""
Teste local do TTS — gera um arquivo .mp3 sem precisar de banco de dados.

Uso:
    python tools/test_tts_local.py
    python tools/test_tts_local.py "Frase personalizada para testar"
"""

import asyncio
import sys
from pathlib import Path

# Adiciona a raiz do projeto ao path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youtube_bot.config import load_settings
from youtube_bot.fun.tts import (
    extract_tts_message,
    generate_tts,
    sanitize_tts_text,
)


async def main() -> None:
    settings = load_settings()

    # ── 1. Teste de parsing do comando ──────────────────────────────
    print("=" * 60)
    print("1. TESTE DE PARSING DO COMANDO !tts")
    print("=" * 60)

    test_messages = [
        "!tts Olá, isso é um teste de voz!",
        "!tts",
        "!tts ",
        "apenas um comentário normal",
        "!TTS Case insensitive",
    ]
    for msg in test_messages:
        extracted = extract_tts_message(msg)
        print(f"  Input : {msg!r}")
        print(f"  Output: {extracted!r}")
        print()

    # ── 2. Teste de sanitização ─────────────────────────────────────
    print("=" * 60)
    print("2. TESTE DE SANITIZAÇÃO DE TEXTO")
    print("=" * 60)

    dirty_texts = [
        "Olá, tudo bem? 😂🔥 <script>alert('xss')</script>",
        "A" * 400,  # texto muito longo
        "Texto com acentos: áéíóú ç ãõ",
    ]
    for text in dirty_texts:
        clean = sanitize_tts_text(text)
        print(f"  Original ({len(text)} chars): {text[:80]}...")
        print(f"  Limpo   ({len(clean)} chars): {clean[:80]}...")
        print()

    # ── 3. Geração real de áudio (gTTS) ─────────────────────────────
    print("=" * 60)
    print("3. GERAÇÃO DE ÁUDIO TTS")
    print("=" * 60)

    # Usa argumento da linha de comando ou frase padrão
    frase = sys.argv[1] if len(sys.argv) > 1 else "Olá! Este é um teste do sistema de voz do bot Gorkzinhaaa."
    print(f"  Provider: {settings.tts_provider}")
    print(f"  Voice  : {settings.tts_voice}")
    print(f"  Frase  : {frase}")
    print()

    try:
        output_dir = Path(settings.tts_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # generate_tts nao usa o banco atualmente, entao None e suficiente
        # para testar o mesmo caminho de provider/cache usado pelo bot.
        audio_path = await generate_tts(frase, settings, db=None, user_id=0)  # type: ignore[arg-type]

        print(f"  ✅ Áudio gerado com sucesso!")
        print(f"  📁 Arquivo: {audio_path}")
        size_kb = Path(audio_path).stat().st_size / 1024
        print(f"  📏 Tamanho: {size_kb:.1f} KB")
        print()
        print("  ▶️  Abra o arquivo acima para ouvir o áudio.")

    except Exception as exc:
        print(f"  ❌ Erro ao gerar áudio: {exc}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 60)
    print("TESTE CONCLUÍDO")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
