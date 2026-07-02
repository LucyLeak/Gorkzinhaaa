from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


QUESTION_KEYWORDS = {"explica", "explique", "porque", "por que", "como", "qual"}
HUMOR_KEYWORDS = {"piada", "engracado", "engracada", "meme", "zoa", "zueira"}


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
