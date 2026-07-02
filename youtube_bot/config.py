from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str
    openai_chat_model: str
    openai_embedding_model: str
    openai_embedding_dimensions: int | None
    database_url: str
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: str
    youtube_api_key: str
    youtube_channel_handle: str
    youtube_video_ids: tuple[str, ...]
    youtube_bot_channel_id: str
    youtube_live_url: str
    youtube_live_connect_message: str
    giphy_api_key: str
    tts_provider: str
    tts_voice: str
    tts_output_dir: str
    tts_cooldown_minutes: int
    memory_retention_days: int
    dry_run: bool
    poll_interval_seconds: int
    max_repair_attempts: int
    coherence_threshold: float
    brain_surprise_chance: float
    forbidden_words: tuple[str, ...]
    log_level: str

    @property
    def has_youtube_oauth(self) -> bool:
        return bool(
            self.youtube_client_id
            and self.youtube_client_secret
            and self.youtube_refresh_token
        )

    @property
    def can_post_to_youtube(self) -> bool:
        return self.has_youtube_oauth and not self.dry_run


def load_settings() -> Settings:
    load_dotenv()

    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=_normalize_openai_base_url(
            os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
        ),
        openai_chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        openai_embedding_model=os.getenv(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
        ),
        openai_embedding_dimensions=_optional_int(
            os.getenv("OPENAI_EMBEDDING_DIMENSIONS")
        ),
        database_url=os.getenv("NEON_DATABASE_URL", ""),
        youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
        youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
        youtube_refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
        youtube_channel_handle=os.getenv("YOUTUBE_CHANNEL_HANDLE", ""),
        youtube_video_ids=_split_csv(os.getenv("YOUTUBE_VIDEO_IDS")),
        youtube_bot_channel_id=os.getenv("YOUTUBE_BOT_CHANNEL_ID", ""),
        youtube_live_url=os.getenv("YOUTUBE_LIVE_URL", ""),
        youtube_live_connect_message=os.getenv(
            "YOUTUBE_LIVE_CONNECT_MESSAGE",
            "Bot conectada ao chat ao vivo.",
        ),
        giphy_api_key=os.getenv("GIPHY_API_KEY", ""),
        tts_provider=os.getenv("TTS_PROVIDER", "gtts"),
        tts_voice=os.getenv("TTS_VOICE", "pt"),
        tts_output_dir=os.getenv("TTS_OUTPUT_DIR", "data/tts_audio"),
        tts_cooldown_minutes=_int(os.getenv("TTS_COOLDOWN_MINUTES"), 10),
        memory_retention_days=_int(os.getenv("MEMORY_RETENTION_DAYS"), 14),
        dry_run=_bool(os.getenv("DRY_RUN"), True),
        poll_interval_seconds=_int(os.getenv("POLL_INTERVAL_SECONDS"), 30),
        max_repair_attempts=_int(os.getenv("MAX_REPAIR_ATTEMPTS"), 3),
        coherence_threshold=_float(os.getenv("COHERENCE_THRESHOLD"), 0.60),
        brain_surprise_chance=_float(os.getenv("BRAIN_SURPRISE_CHANCE"), 0.20),
        forbidden_words=_split_csv(os.getenv("FORBIDDEN_WORDS")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def _normalize_openai_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized
