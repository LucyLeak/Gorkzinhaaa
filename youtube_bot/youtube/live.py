from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from youtube_bot.youtube.client import YouTubeClient
from youtube_bot.utils.helpers import parse_youtube_datetime

logger = logging.getLogger(__name__)


@dataclass
class YouTubeLiveMessage:
    message_id: str
    author_channel_id: str
    author_name: str
    text: str
    published_at: datetime


class LiveChatClient:
    def __init__(self, youtube_client: YouTubeClient) -> None:
        self.youtube_client = youtube_client

    async def get_messages(
        self,
        live_chat_id: str,
        page_token: str | None = None,
    ) -> tuple[list[YouTubeLiveMessage], str | None, int]:
        service = self.youtube_client._build_service()
        payload = await self.youtube_client._call_api(
            self._list_messages,
            service,
            live_chat_id,
            page_token,
        )
        messages = []
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            messages.append(
                YouTubeLiveMessage(
                    message_id=item["id"],
                    author_channel_id=author.get("channelId", ""),
                    author_name=author.get("displayName", "usuario"),
                    text=snippet.get("displayMessage", ""),
                    published_at=parse_youtube_datetime(snippet["publishedAt"]),
                )
            )
        return (
            messages,
            payload.get("nextPageToken"),
            int(payload.get("pollingIntervalMillis", 5000)),
        )

    def _list_messages(self, service, live_chat_id: str, page_token: str | None):
        return (
            service.liveChatMessages()
            .list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=page_token,
            )
            .execute()
        )

    async def post_message(self, live_chat_id: str, text: str, force: bool = False) -> str | None:
        if self.youtube_client.settings.dry_run and not force:
            logger.info("DRY_RUN: mensagem para live %s: %s", live_chat_id, text)
            return None
        if not self.youtube_client.settings.has_youtube_oauth:
            raise RuntimeError("OAuth completo e necessario para enviar mensagem na live.")

        service = self.youtube_client._build_service()
        payload = await self.youtube_client._call_api(self._insert_message, service, live_chat_id, text)
        return payload.get("id")

    def _insert_message(self, service, live_chat_id: str, text: str):
        return (
            service.liveChatMessages()
            .insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": text},
                    }
                },
            )
            .execute()
        )
