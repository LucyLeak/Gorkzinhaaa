from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from youtube_bot.config import Settings
from youtube_bot.db import models
from youtube_bot.db.pool import Database

logger = logging.getLogger(__name__)

# Limite de caracteres por solicitacao TTS (evita abuso)
MAX_TTS_CHARS = 300
# Caracteres permitidos no texto (alfanumerico, pontuacao basica, espaco, acentos)
_TTS_SAFE_PATTERN = re.compile(r"[^a-zA-Z0-9áàâãéèêíìóòôõúùûçÁÀÂÃÉÈÊÍÌÓÒÔÕÚÙÛÇ\s.,!?;:\-()\"'@#&%$+=*/<>\[\]{}|~^_]")
# Prefixo do comando
TTS_PREFIX = "!tts"
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"


def sanitize_tts_text(text: str) -> str:
    """Remove caracteres perigosos e limita o tamanho do texto para TTS."""
    cleaned = _TTS_SAFE_PATTERN.sub("", text.strip())
    if len(cleaned) > MAX_TTS_CHARS:
        cleaned = cleaned[:MAX_TTS_CHARS].rsplit(" ", 1)[0]
    return cleaned


def extract_tts_message(message: str) -> str | None:
    """Extrai a mensagem apos o comando !tts. Retorna None se nao for comando TTS."""
    normalized = message.strip()
    if not normalized.lower().startswith(TTS_PREFIX):
        return None
    text = normalized[len(TTS_PREFIX):].strip()
    if not text:
        return None
    return text


def _text_hash(text: str) -> str:
    """Gera um hash curto do texto para nome do arquivo."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


async def generate_tts(
    text: str,
    settings: Settings,
    db: Database,
    user_id: int,
    voice: str | None = None,
) -> str:
    """
    Gera audio TTS a partir do texto e salva em disco.
    Retorna o caminho do arquivo .mp3 gerado.
    """
    output_dir = Path(settings.tts_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    file_hash = _text_hash(text)
    file_path = output_dir / f"tts_{file_hash}.mp3"

    # Cache: se o arquivo ja existe, reutiliza
    if file_path.exists():
        logger.info("Audio TTS ja existe em cache: %s", file_path)
        return str(file_path)

    if settings.tts_provider == "openai":
        return await _generate_openai_tts(text, file_path, settings, voice=voice)
    else:
        return await _generate_gtts(text, file_path, settings, voice=voice)


async def _generate_gtts(text: str, file_path: Path, settings: Settings, voice: str | None = None) -> str:
    """Gera audio usando gTTS (Google Text-to-Speech)."""
    from gtts import gTTS

    lang = voice or settings.tts_voice
    lang = lang if lang in {"pt", "pt-br", "en", "es"} else "pt"
    tts = gTTS(text=text, lang=lang, slow=False)
    await _run_save(tts, file_path)
    logger.info("Audio TTS (gTTS) salvo em: %s", file_path)
    return str(file_path)


async def _generate_openai_tts(text: str, file_path: Path, settings: Settings, voice: str | None = None) -> str:
    """Gera audio usando OpenAI TTS API."""
    import asyncio

    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )
    selected_voice = voice or settings.tts_voice
    selected_voice = selected_voice if selected_voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"} else "nova"

    response = await client.audio.speech.create(
        model="tts-1",
        voice=selected_voice,
        input=text,
    )
    # stream_to_file is blocking — run in thread to avoid blocking the event loop
    await asyncio.to_thread(response.stream_to_file, str(file_path))

    logger.info("Audio TTS (OpenAI) salvo em: %s", file_path)
    return str(file_path)


async def _run_save(tts, file_path: Path) -> None:
    """Salva o audio gTTS em thread separada (bloqueante)."""
    import asyncio
    await asyncio.to_thread(tts.save, str(file_path))


def _is_public_catbox_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    host = parsed.netloc.lower()
    return (
        parsed.scheme == "https"
        and (host == "catbox.moe" or host.endswith(".catbox.moe"))
        and bool(parsed.path.strip("/"))
    )


async def _upload_to_catbox(file_path: str) -> str | None:
    """Faz upload do MP3 para Catbox.moe e retorna a URL publica HTTPS."""
    import asyncio

    import aiohttp

    path = Path(file_path)
    if not path.is_file():
        logger.warning("Arquivo TTS nao encontrado para upload no Catbox: %s", path)
        return None

    timeout = aiohttp.ClientTimeout(total=60)
    try:
        with path.open("rb") as audio_file:
            form = aiohttp.FormData()
            form.add_field("reqtype", "fileupload")
            form.add_field(
                "fileToUpload",
                audio_file,
                filename=path.name,
                content_type="audio/mpeg",
            )

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(CATBOX_UPLOAD_URL, data=form) as response:
                    status = response.status
                    body = (await response.text()).strip()

        if status != 200:
            logger.warning(
                "Catbox upload falhou: status=%s body=%s",
                status,
                body[:200],
            )
            return None

        if _is_public_catbox_url(body):
            return body

        logger.warning("Catbox retornou resposta inesperada: %s", body[:200])
        return None
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.exception("Erro ao fazer upload para Catbox: %s", exc)
        return None


async def handle_tts_command(
    message: str,
    user_id: int,
    settings: Settings,
    db: Database,
) -> str | None:
    """
    Processa o comando !tts.
    Retorna a mensagem de resposta ou None se nao for comando TTS.
    """
    raw_text = extract_tts_message(message)
    if raw_text is None:
        return None

    texto_falado = sanitize_tts_text(raw_text)
    if not texto_falado:
        return "!tts: O texto ficou vazio apos limpeza. Tente algo diferente."

    # --- Rate limit: verifica ultima solicitacao TTS do usuario no banco ---
    last_tts_iso = await models.get_last_tts_time(db, user_id)
    if last_tts_iso is not None:
        last_tts = datetime.fromisoformat(last_tts_iso)
        cooldown = timedelta(minutes=settings.tts_cooldown_minutes)
        remaining = last_tts + cooldown - datetime.now(timezone.utc)
        if remaining.total_seconds() > 0:
            minutos = int(remaining.total_seconds() // 60)
            segundos = int(remaining.total_seconds() % 60)
            return (
                f"Aguarde {minutos}m {segundos}s para usar !tts novamente. "
                f"(Limite: 1 a cada {settings.tts_cooldown_minutes} min)"
            )

    # Insere na database como pendente
    tts_id = await models.insert_tts_request(db, user_id, raw_text, texto_falado)
    await models.update_tts_status(db, tts_id, "processando")

    try:
        audio_path = await generate_tts(texto_falado, settings, db, user_id)

        # Upload to Catbox for a public HTTPS URL
        catbox_url = await _upload_to_catbox(audio_path)
        if catbox_url:
            await models.update_tts_status(db, tts_id, "concluido", audio_url=catbox_url)
            try:
                Path(audio_path).unlink(missing_ok=True)
            except OSError:
                pass
            return f"Audio TTS gerado: {catbox_url}"

        await models.update_tts_status(
            db,
            tts_id,
            "erro",
            erro="Falha ao enviar audio TTS para o Catbox.",
        )
        return "Falha ao enviar audio TTS para o Catbox. Tente novamente mais tarde."
    except Exception as exc:
        logger.exception("Erro ao gerar TTS para tts_id=%s", tts_id)
        await models.update_tts_status(db, tts_id, "erro", erro=str(exc))
        return "Falha ao gerar audio TTS. Tente novamente mais tarde."
