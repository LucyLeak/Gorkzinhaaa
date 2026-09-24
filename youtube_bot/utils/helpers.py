from __future__ import annotations

import json
import logging
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
_JSON_FRAGMENT_LINE_PATTERN = re.compile(r'^\s*["\']?(?:thought|message)["\']?\s*:', re.IGNORECASE)
# Heuristica anti-leak: texto puro que comeca com padroes de raciocinio
# ("pensando em voz alta") e bloqueado em vez de publicado no chat.
_LEAK_PATTERNS = re.compile(
    r"^\s*(?:"
    r"vou|devo|preciso|o usuário|o usuario|o pedido|analisando|pensando|"
    r"deixa eu|talvez|primeiro|então|entao|aqui está o raciocínio|"
    r"aqui esta o raciocinio|vou responder|devo responder|o melhor é|o melhor e"
    r")\b",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


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


def sanitize_for_chat(text: str) -> str:
    """Remove formatting or structured-output fragments that must not reach chat."""
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
    cleaned = "\n".join(
        line for line in cleaned.splitlines()
        if not _JSON_FRAGMENT_LINE_PATTERN.match(line)
    )
    return cleaned.strip()


def prepare_chat_message(
    text: str,
    max_chars: int = MAX_CHAT_MESSAGE_CHARS,
    allow_plain_text: bool = True,
    anti_leak: bool = True,
) -> tuple[str, str]:
    """Parse estruturado primeiro, depois tags <think>; texto puro por último.

    Fluxo do branch não-JSON (raw não começa com "{"):
    - allow_plain_text=False  -> fail-closed: bloqueia texto puro ("", "").
      Reservado para quem QUER garantir saída estruturada.
    - anti_leak=True e o texto parece raciocínio em voz alta
      (_LEAK_PATTERNS)       -> bloqueado ("", "") para evitar vazamento.
    - caso contrário          -> publicado como mensagem final, com aviso
      "Plain-text fallback used for brain reply." (é o caminho do cérebro no
      parse único: Brain.generate chama com os defaults).
    """
    if not text:
        return "", ""

    raw = text.strip()
    json_candidate = raw
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        json_candidate = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(json_candidate)
    except json.JSONDecodeError:
        if raw.startswith("{"):
            if re.search(r'["\']?thought["\']?\s*:', raw, re.IGNORECASE):
                logger.error("Resposta estruturada invalida contem thought; bloqueando postagem.")
            else:
                logger.error("Resposta da IA parece JSON malformado; bloqueando postagem.")
            return "", ""
        logger.warning("Resposta da IA nao esta em JSON; usando parser legado.")
        thought, message = parse_thinking_response(raw)
        if thought:
            return sanitize_for_chat(thought), limit_chat_message(
                sanitize_for_chat(message), max_chars
            )
        if _THINK_BLOCK_PATTERN.search(raw) or _UNCLOSED_THINK_PATTERN.search(raw):
            # Sobrou apenas conteudo dentro de tags de pensamento; nada publicavel.
            return "", ""
        if not allow_plain_text:
            logger.warning("Plain-text fallback blocked by policy.")
            return "", ""
        if anti_leak and _LEAK_PATTERNS.match(message):
            logger.warning("Plain-text looks like reasoning; blocking to avoid leak.")
            return "", ""
        logger.warning("Plain-text fallback used for brain reply.")
        return "", limit_chat_message(sanitize_for_chat(message), max_chars)

    if not isinstance(data, dict):
        logger.warning("Resposta JSON da IA nao e um objeto; bloqueando postagem.")
        return "", ""

    thought = data.get("thought", "")
    message = data.get("message", "")
    if not isinstance(thought, str) or not isinstance(message, str):
        logger.warning("Resposta JSON da IA possui thought/message invalidos; bloqueando postagem.")
        return "", ""
    return sanitize_for_chat(thought), limit_chat_message(
        sanitize_for_chat(message), max_chars
    )
