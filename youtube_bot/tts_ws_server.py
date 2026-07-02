from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database

logger = logging.getLogger(__name__)


class TtsWebSocketServer:
    """
    WebSocket server that polls the database for concluded TTS requests
    and broadcasts the Catbox URLs to all connected clients.

    Runs inside the same process as the main bot, so it inherits the bot's
    host/port and can be reached publicly (e.g. via Railway's HTTPS proxy).
    """

    def __init__(
        self,
        db: Database,
        host: str = "0.0.0.0",
        port: int = 8765,
        poll_interval: float = 2.0,
    ) -> None:
        self.db = db
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self._clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._site: web.TCPSite | None = None

        # Routes
        self._app.router.add_get("/", self._handle_health)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)

    async def start(self) -> None:
        """Start the HTTP server and the DB poller background task."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            "TTS WebSocket server started on ws://%s:%s/ws",
            self.host,
            self.port,
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the poller and shut down the HTTP server."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._runner:
            await self._runner.cleanup()
        logger.info("TTS WebSocket server stopped.")

    # ── Handlers ──────────────────────────────────────────────────────

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info("🔌 TTS WS client connected (total: %d)", len(self._clients))
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data == "ping":
                        await ws.send_str("pong")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("TTS WS error: %s", ws.exception())
        finally:
            self._clients.discard(ws)
            logger.info("🔌 TTS WS client disconnected (total: %d)", len(self._clients))
        return ws

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "clients": len(self._clients)})

    # ── Poller ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Continuously poll the DB for new concluded TTS requests."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("TTS WS poll error, retrying in %ds.", self.poll_interval)
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        if not self._clients:
            return  # nobody connected, skip the query

        rows = await models.get_pending_tts(self.db, limit=10)
        for row in rows:
            audio_url = row.get("audio_url") or ""
            payload = json.dumps({
                "id": row["id"],
                "username": row.get("autor") or "",
                "message": row.get("texto_falado") or "",
                "type": "url",
                "audio": audio_url,
            })

            # Broadcast to all connected clients
            dead: list[web.WebSocketResponse] = []
            for ws in self._clients:
                try:
                    await ws.send_str(payload)
                except (ConnectionError, asyncio.TimeoutError):
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

            # Mark as reproduzido
            await models.mark_tts_reproduzido(self.db, row["id"])

            if row.get("texto_falado"):
                logger.info(
                    "📢 TTS #%d broadcasted: \"%s...\"",
                    row["id"],
                    str(row["texto_falado"])[:60],
                )
