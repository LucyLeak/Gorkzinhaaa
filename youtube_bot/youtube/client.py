from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from youtube_bot.config import Settings
from youtube_bot.utils.helpers import parse_youtube_datetime

logger = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/youtube.force-ssl",)
TOKEN_URI = "https://oauth2.googleapis.com/token"


class YouTubeQuotaExceededError(RuntimeError):
    """Raised when the YouTube API quota bucket is exhausted."""


@dataclass
class YouTubeComment:
    comment_id: str
    video_id: str
    author_channel_id: str
    author_name: str
    text: str
    published_at: datetime


class YouTubeClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = None

    def reset_service(self) -> None:
        """Forca a recriacao do servico na proxima chamada (util apos erros SSL)."""
        self._service = None

    async def _call_api(self, func, *args, max_ssl_retries: int = 2):
        """Executa uma chamada a API em thread, com retry automatico em erros SSL."""
        for attempt in range(max_ssl_retries + 1):
            try:
                return await asyncio.to_thread(func, *args)
            except HttpError as exc:
                if _is_quota_exceeded(exc):
                    raise YouTubeQuotaExceededError(_http_error_message(exc)) from exc
                raise
            except ssl.SSLError:
                if attempt < max_ssl_retries:
                    logger.debug("SSL error, resetting HTTP connection (retry %d/%d)...", attempt + 1, max_ssl_retries)
                    self.reset_service()
                    self._build_service()
                else:
                    raise

    def _build_service(self):
        if self._service is not None:
            return self._service

        if self.settings.has_youtube_oauth:
            credentials = Credentials(
                token=None,
                refresh_token=self.settings.youtube_refresh_token,
                token_uri=TOKEN_URI,
                client_id=self.settings.youtube_client_id,
                client_secret=self.settings.youtube_client_secret,
                scopes=SCOPES,
            )
            credentials.refresh(Request())
            self._service = build(
                "youtube", "v3", credentials=credentials, cache_discovery=False
            )
            return self._service

        if self.settings.youtube_api_key:
            self._service = build(
                "youtube",
                "v3",
                developerKey=self.settings.youtube_api_key,
                cache_discovery=False,
            )
            return self._service

        raise RuntimeError(
            "Configure YOUTUBE_API_KEY para leitura ou OAuth completo para leitura/postagem."
        )

    async def get_new_comments(
        self,
        video_id: str,
        after_timestamp: datetime | None,
        max_pages: int = 3,
    ) -> list[YouTubeComment]:
        service = self._build_service()
        comments: list[YouTubeComment] = []
        page_token: str | None = None

        for _ in range(max_pages):
            payload = await self._call_api(
                self._list_comment_page,
                service,
                video_id,
                page_token,
            )
            for item in payload.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                published_at = parse_youtube_datetime(snippet["publishedAt"])
                if after_timestamp and published_at <= after_timestamp:
                    continue
                author_channel = snippet.get("authorChannelId", {}).get("value", "")
                comments.append(
                    YouTubeComment(
                        comment_id=item["snippet"]["topLevelComment"]["id"],
                        video_id=video_id,
                        author_channel_id=author_channel,
                        author_name=snippet.get("authorDisplayName", "usuario"),
                        text=snippet.get("textDisplay") or snippet.get("textOriginal") or "",
                        published_at=published_at,
                    )
                )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return sorted(comments, key=lambda comment: comment.published_at)

    def _list_comment_page(self, service, video_id: str, page_token: str | None):
        return (
            service.commentThreads()
            .list(
                part="snippet",
                videoId=video_id,
                order="time",
                textFormat="plainText",
                maxResults=50,
                pageToken=page_token,
            )
            .execute()
        )

    async def get_active_live_chat_id(self, video_id: str) -> str:
        service = self._build_service()
        payload = await self._call_api(self._get_video_live_details, service, video_id)
        items = payload.get("items", [])
        if not items:
            raise RuntimeError(f"Video nao encontrado: {video_id}")

        details = items[0].get("liveStreamingDetails") or {}
        live_chat_id = details.get("activeLiveChatId")
        if not live_chat_id:
            raise RuntimeError(
                "Nao encontrei chat ativo para esse video. "
                "Confira se a live esta ao vivo e com chat habilitado."
            )
        return live_chat_id

    def _get_video_live_details(self, service, video_id: str):
        return (
            service.videos()
            .list(part="liveStreamingDetails,snippet", id=video_id)
            .execute()
        )

    async def get_authenticated_channel_id(self) -> str | None:
        if not self.settings.has_youtube_oauth:
            return None
        service = self._build_service()
        payload = await self._call_api(self._get_my_channel, service)
        items = payload.get("items", [])
        if not items:
            return None
        return items[0].get("id")

    def _get_my_channel(self, service):
        return service.channels().list(part="id", mine=True).execute()

    async def post_reply(self, comment_id: str, text: str) -> str | None:
        if self.settings.dry_run:
            logger.info("DRY_RUN: resposta para %s: %s", comment_id, text)
            return None
        if not self.settings.has_youtube_oauth:
            raise RuntimeError("OAuth completo e necessario para postar respostas.")

        service = self._build_service()
        payload = await self._call_api(self._insert_reply, service, comment_id, text)
        return payload.get("id")

    def _insert_reply(self, service, comment_id: str, text: str):
        return (
            service.comments()
            .insert(
                part="snippet",
                body={"snippet": {"parentId": comment_id, "textOriginal": text}},
            )
            .execute()
        )

    # ── Channel & Live Detection ──────────────────────────────────────

    async def resolve_channel_id(self, handle: str) -> str | None:
        """Resolve um @handle do YouTube para o channel ID usando channels.list com forHandle."""
        service = self._build_service()
        clean_handle = handle.lstrip("@")
        payload = await self._call_api(
            self._resolve_channel_by_handle, service, clean_handle
        )
        items = payload.get("items", [])
        if not items:
            logger.warning("Canal nao encontrado para o handle: @%s", clean_handle)
            return None
        channel_id = items[0]["id"]
        logger.info("Handle @%s resolvido para channel ID: %s", clean_handle, channel_id)
        return channel_id

    def _resolve_channel_by_handle(self, service, handle: str):
        return (
            service.channels()
            .list(
                part="id",
                forHandle=handle,
                maxResults=1,
            )
            .execute()
        )

    async def get_active_lives(self, channel_id: str) -> list[dict]:
        """Retorna lista de lives ativas de um canal.
        Cada item contem: video_id, title, live_chat_id."""
        service = self._build_service()
        payload = await self._call_api(
            self._search_active_lives, service, channel_id
        )
        lives: list[dict] = []
        for item in payload.get("items", []):
            video_id = item["id"]["videoId"]
            snippet = item.get("snippet", {})
            # Busca detalhes da live para obter o liveChatId
            try:
                details_payload = await self._call_api(
                    self._get_video_live_details, service, video_id
                )
                video_items = details_payload.get("items", [])
                if video_items:
                    live_details = video_items[0].get("liveStreamingDetails") or {}
                    live_chat_id = live_details.get("activeLiveChatId")
                    if live_chat_id:
                        lives.append({
                            "video_id": video_id,
                            "title": snippet.get("title", "Sem titulo"),
                            "live_chat_id": live_chat_id,
                        })
            except Exception:
                logger.exception("Falha ao obter detalhes da live %s", video_id)
        return lives

    def _search_active_lives(self, service, channel_id: str):
        return (
            service.search()
            .list(
                part="snippet",
                channelId=channel_id,
                eventType="live",
                type="video",
                maxResults=10,
            )
            .execute()
        )


def _is_quota_exceeded(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    content = _decode_http_error_content(exc).lower()
    return status in {403, 429} and (
        "quota exceeded" in content
        or "quotaexceeded" in content
        or "ratelimitexceeded" in content
    )


def _http_error_message(exc: HttpError) -> str:
    content = _decode_http_error_content(exc).strip()
    if content:
        return content[:500]
    return str(exc)


def _decode_http_error_content(exc: HttpError) -> str:
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)
