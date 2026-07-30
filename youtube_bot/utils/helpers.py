from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


QUESTION_KEYWORDS = {"explica", "explique", "porque", "por que", "como", "qual"}
HUMOR_KEYWORDS = {"piada", "engracado", "engracada", "meme", "zoa", "zueira"}
MAX_CHAT_MESSAGE_CHARS = 150
_THINK_TAG_NAMES = "think|thinking|thought|analysis"
_THINK_BLOCK_PATTERN = re.compile(
    rf"<(?:{_THINK_TAG_NAMES})\b[^>]*>(.*?)</(?:{_THINK_TAG_NAMES})>",
    re.DOTALL | re.IGNORECASE,
)
_UNCLOSED_THINK_PATTERN = re.compile(
    rf"<(?:{_THINK_TAG_NAMES})\b[^>]*>.*$",
    re.DOTALL | re.IGNORECASE,
)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def has_any_keyword(text: str, keywords: set[str]) -> bool:
    normalized = normalize_text(text)
    return any(keyword in normalized for keyword in keywords)


def parse_youtube_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def extract_youtube_video_id(value: str) -> str | None:
    candidate = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return candidate

    parsed = urlparse(candidate)
    host = parsed.netloc.lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be" and path_parts:
        return _valid_video_id(path_parts[0])

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        query_video_id = parse_qs(parsed.query).get("v", [None])[0]
        if query_video_id:
            return _valid_video_id(query_video_id)
        if len(path_parts) >= 2 and path_parts[0] in {"live", "shorts", "embed", "v"}:
            return _valid_video_id(path_parts[1])

    match = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})", candidate)
    if match:
        return match.group(1)
    return None


def _valid_video_id(value: str | None) -> str | None:
    if value and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    return None


def parse_thinking_response(text: str) -> tuple[str, str]:
    """
    Parses a response that may contain <think>...</think> tags.

    Returns:
        A tuple of (thought, message).
        If no tags are found, thought is an empty string and message is the original text.
    """
    if not text:
        return "", ""

    thoughts = [match.group(1).strip() for match in _THINK_BLOCK_PATTERN.finditer(text)]
    message = _THINK_BLOCK_PATTERN.sub("", text)

    unclosed = _UNCLOSED_THINK_PATTERN.search(message)
    if unclosed:
        thoughts.append(unclosed.group(0).strip())
        message = message[: unclosed.start()]

    return "\n\n".join(part for part in thoughts if part), message.strip()


def limit_chat_message(text: str, max_chars: int = MAX_CHAT_MESSAGE_CHARS) -> str:
    """Normaliza e limita a mensagem que sera enviada ao YouTube."""
    message = _WHITESPACE_PATTERN.sub(" ", text).strip()
    if len(message) <= max_chars:
        return message

    suffix = "..."
    cutoff = max_chars - len(suffix)
    truncated = message[:cutoff].rsplit(" ", 1)[0].rstrip(" .,;:-")
    if len(truncated) < max_chars // 2:
        truncated = message[:cutoff].rstrip(" .,;:-")
    return f"{truncated}{suffix}"


def prepare_chat_message(text: str, max_chars: int = MAX_CHAT_MESSAGE_CHARS) -> tuple[str, str]:
    """Remove thinking e retorna (thought, mensagem_final) pronta para chat."""
    thought, message = parse_thinking_response(text)
    return thought, limit_chat_message(message, max_chars)
