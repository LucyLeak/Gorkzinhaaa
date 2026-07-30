from __future__ import annotations

import asyncio
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
SUPPORTED_TTS_PROVIDERS = {"gtts", "edge", "edge-tts", "edge_tts", "openai", "elevenlabs"}
# Caracteres removidos do texto falado (controle, emojis e simbolos incomuns)
_TTS_UNSAFE_PATTERN = re.compile(r"[^a-zA-Z0-9À-ÖØ-öø-ÿ\s.,!?;:\-()\"'@#&%$+=*/]")
_WHITESPACE_PATTERN = re.compile(r"\s+")
# Prefixo do comando
TTS_PREFIX = "!tts"
_TTS_COMMAND_PATTERN = re.compile(r"^\s*!tts(?:\s+(.+))?\s*$", re.IGNORECASE | re.DOTALL)
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"

_GTTS_LANG_ALIASES = {
    "pt": "pt",
    "pt-br": "pt",
    "br": "pt",
    "en": "en",
    "en-us": "en",
    "es": "es",
}

_EDGE_VOICE_ALIASES = {
    "pt": "pt-BR-FranciscaNeural",
    "pt-br": "pt-BR-FranciscaNeural",
    "br": "pt-BR-FranciscaNeural",
    "pt-br-female": "pt-BR-FranciscaNeural",
    "pt-br-male": "pt-BR-AntonioNeural",
    "en": "en-US-AriaNeural",
    "en-us": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
}


def sanitize_tts_text(text: str) -> str:
    """Remove caracteres perigosos e limita o tamanho do texto para TTS."""
    cleaned = _TTS_UNSAFE_PATTERN.sub("", text.strip())
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    if len(cleaned) > MAX_TTS_CHARS:
        cleaned = cleaned[:MAX_TTS_CHARS].rsplit(" ", 1)[0]
    return cleaned


def extract_tts_message(message: str) -> str | None:
    """Extrai a mensagem apos o comando !tts. Retorna None se nao for comando TTS."""
    match = _TTS_COMMAND_PATTERN.match(message)
    if not match:
        return None
    text = (match.group(1) or "").strip()
    if not text:
        return None
    return text


def _text_hash(text: str) -> str:
    """Gera um hash curto do texto para nome do arquivo."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized == "edge-tts":
        return "edge"
    return normalized


def _provider_or_raise(settings: Settings) -> str:
    provider = _normalize_provider(settings.tts_provider or "gtts")
    if provider not in {"gtts", "edge", "openai", "elevenlabs"}:
        allowed = ", ".join(("gtts", "edge", "elevenlabs", "openai"))
        raise ValueError(f"TTS_PROVIDER invalido: {settings.tts_provider!r}. Use: {allowed}.")
    return provider


def _selected_voice(provider: str, settings: Settings, voice: str | None = None) -> str:
    requested = (voice or settings.tts_voice or "").strip()
    key = requested.lower()
    if provider == "gtts":
        return _GTTS_LANG_ALIASES.get(key, "pt")
    if provider == "edge":
        return _EDGE_VOICE_ALIASES.get(key, requested or _EDGE_VOICE_ALIASES["pt-br"])
    if provider == "elevenlabs":
        return requested or settings.elevenlabs_voice_id or DEFAULT_ELEVENLABS_VOICE_ID
    selected = requested or "nova"
    return selected if selected in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"} else "nova"


def _cache_hash(text: str, provider: str, voice: str, model: str = "") -> str:
    cache_key = "|".join((provider, voice, model, text))
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:12]


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

    provider = _provider_or_raise(settings)
    selected_voice = _selected_voice(provider, settings, voice)
    model_id = settings.elevenlabs_model_id if provider == "elevenlabs" else ""
    file_hash = _cache_hash(text, provider, selected_voice, model_id)
    file_path = output_dir / f"tts_{file_hash}.mp3"

    # Cache: se o arquivo ja existe, reutiliza
    if file_path.exists():
        logger.info("Audio TTS ja existe em cache: %s", file_path)
        return str(file_path)

    if provider == "elevenlabs":
        return await _generate_elevenlabs_tts(text, file_path, settings, selected_voice)
    if provider == "edge":
        return await _generate_edge_tts(text, file_path, selected_voice)
    if provider == "openai":
        return await _generate_openai_tts(text, file_path, settings, selected_voice)
    return await _generate_gtts(text, file_path, selected_voice)


async def _generate_gtts(text: str, file_path: Path, voice: str) -> str:
    """Gera audio usando gTTS (Google Text-to-Speech)."""
    from gtts import gTTS

    tts = gTTS(text=text, lang=voice, slow=False)
    await _run_save(tts, file_path)
    logger.info("Audio TTS (gTTS) salvo em: %s", file_path)
    return str(file_path)


async def _generate_edge_tts(text: str, file_path: Path, voice: str) -> str:
    """Gera audio usando Microsoft Edge TTS (sem chave de API)."""
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Provider TTS 'edge' requer a dependencia edge-tts. "
            "Rode: pip install -r requirements.txt"
        ) from exc

    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(str(file_path))
    logger.info("Audio TTS (Edge) salvo em: %s", file_path)
    return str(file_path)


async def _generate_elevenlabs_tts(
    text: str,
    file_path: Path,
    settings: Settings,
    voice_id: str,
) -> str:
    """Gera audio usando a API Text-to-Speech da ElevenLabs."""
    import aiohttp

    api_key = settings.elevenlabs_api_key.strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY nao configurada para TTS_PROVIDER=elevenlabs.")

    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    params = {"output_format": settings.elevenlabs_output_format or "mp3_44100_128"}
    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model_id or "eleven_flash_v2_5",
    }
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, params=params, json=payload, headers=headers) as response:
            body = await response.read()
            if response.status >= 400:
                detail = body.decode("utf-8", errors="replace")[:300]
                raise RuntimeError(
                    f"ElevenLabs TTS falhou: status={response.status} body={detail}"
                )

    await asyncio.to_thread(file_path.write_bytes, body)
    logger.info("Audio TTS (ElevenLabs) salvo em: %s", file_path)
    return str(file_path)


async def _generate_openai_tts(text: str, file_path: Path, settings: Settings, voice: str) -> str:
    """Gera audio usando OpenAI TTS API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )
    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice,
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


async def upload_tts_audio(audio_path: str, settings: Settings) -> str | None:
    """Tenta upload para Catbox (ate 2 tentativas). Se falhar, retorna URL local.

    Retorna a URL publica (Catbox ou local) ou None se o arquivo nao existir.
    """
    path = Path(audio_path)
    if not path.is_file():
        logger.warning("Arquivo TTS nao encontrado: %s", path)
        return None

    # Try Catbox up to 2 times
    for attempt in (1, 2):
        url = await _upload_to_catbox(audio_path)
        if url:
            return url
        if attempt < 2:
            logger.warning("Catbox tentativa %d falhou, retentando...", attempt)
            await asyncio.sleep(1.0)

    # Catbox failed both times — fall back to local URL
    base = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
    if base:
        local_url = f"{base}/audio/{path.name}"
        logger.warning("Catbox indisponivel, usando URL local: %s", local_url)
        return local_url

    logger.warning("Catbox indisponivel e PUBLIC_BASE_URL nao configurada — sem fallback.")
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

        # Upload with retry (Catbox 2x, then local fallback)
        public_url = await upload_tts_audio(audio_path, settings)
        if public_url:
            await models.update_tts_status(db, tts_id, "concluido", audio_url=public_url)
            return f"Audio TTS gerado: {public_url}"

        await models.update_tts_status(
            db,
            tts_id,
            "erro",
            erro="Falha ao enviar audio TTS (Catbox offline e sem fallback local).",
        )
        return "Falha ao enviar audio TTS. Tente novamente mais tarde."
    except Exception as exc:
        logger.exception("Erro ao gerar TTS para tts_id=%s", tts_id)
        await models.update_tts_status(db, tts_id, "erro", erro=str(exc))
        return "Falha ao gerar audio TTS. Tente novamente mais tarde."
