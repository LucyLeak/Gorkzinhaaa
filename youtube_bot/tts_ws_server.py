from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database

if TYPE_CHECKING:
    from youtube_bot.config import Settings

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
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.settings = settings
        self._clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._site: web.TCPSite | None = None

        # Routes
        self._app.router.add_get("/", self._handle_health)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/pending-tts", self._handle_pending_tts)
        self._app.router.add_get("/tts-test", self._handle_tts_test)
        # Serve local TTS audio files as fallback when Catbox is down
        self._app.router.add_static("/audio/", Path("data/tts_audio"), show_index=False)

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
            await self._broadcast_tts(
                tts_id=row["id"],
                username=str(row.get("autor") or ""),
                message=str(row.get("texto_falado") or ""),
                audio_url=str(row.get("audio_url") or ""),
            )

    # ── HTTP test endpoints ─────────────────────────────────────────

    async def _handle_pending_tts(self, request: web.Request) -> web.Response:
        """GET /pending-tts — Lista TTS concluídos pendentes de broadcast."""
        rows = await models.get_pending_tts(self.db, limit=20)
        return web.json_response({
            "count": len(rows),
            "clients_connected": len(self._clients),
            "items": [
                {
                    "id": r["id"],
                    "username": r.get("autor"),
                    "message": r.get("texto_falado"),
                    "audio_url": r.get("audio_url"),
                }
                for r in rows
            ],
        })

    async def _handle_tts_test(self, request: web.Request) -> web.Response:
        """GET /tts-test?text=... — Gera um TTS de teste e faz broadcast via WebSocket.

        Query params:
            text  — Frase para sintetizar (default: "Teste TTS via HTTP")
            voice — Voz (opcional, sobrescreve config)
        """
        if self.settings is None:
            return web.json_response(
                {"error": "TTS settings not configured on this server"},
                status=503,
            )

        from youtube_bot.fun.tts import generate_tts, sanitize_tts_text, upload_tts_audio

        text = request.query.get("text", "Teste TTS via HTTP do bot Gorkzinhaaa!")
        voice = request.query.get("voice")
        texto_falado = sanitize_tts_text(text)

        try:
            # Ensure a system user exists for TTS test requests
            sys_user = await models.upsert_user(self.db, "__tts_system__", "TTS System")
            sys_user_id = int(sys_user["id"])

            # Insert into DB so the poller can broadcast it
            tts_id = await models.insert_tts_request(self.db, sys_user_id, text, texto_falado)
            await models.update_tts_status(self.db, tts_id, "processando")

            audio_path = await generate_tts(texto_falado, self.settings, self.db, user_id=sys_user_id, voice=voice)
            public_url = await upload_tts_audio(audio_path, self.settings)

            # Only delete local file if uploaded to Catbox (not local fallback)
            if public_url and "catbox.moe" in public_url:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except OSError:
                    pass

            if public_url:
                await models.update_tts_status(self.db, tts_id, "concluido", audio_url=public_url)
                # Immediately broadcast to all connected clients
                await self._broadcast_tts(tts_id, "HTTP Tester", texto_falado, public_url)
                return web.json_response({
                    "ok": True,
                    "text": text,
                    "audio_url": public_url,
                    "provider": self.settings.tts_provider,
                    "voice": voice or self.settings.tts_voice,
                })
            else:
                await models.update_tts_status(self.db, tts_id, "erro", erro="Falha ao enviar audio TTS (Catbox offline e sem fallback local).")
                return web.json_response({
                    "ok": False,
                    "error": "Catbox upload failed and no local fallback available",
                    "local_path": audio_path,
                }, status=502)
        except Exception as exc:
            logger.exception("TTS test endpoint error")
            return web.json_response({
                "ok": False,
                "error": str(exc),
            }, status=500)

    async def _broadcast_tts(
        self, tts_id: int, username: str, message: str, audio_url: str
    ) -> None:
        """Send a TTS payload to all connected WebSocket clients immediately."""
        if not self._clients:
            return
        payload = json.dumps({
            "id": tts_id,
            "username": username,
            "message": message,
            "type": "url",
            "audio": audio_url,
        })
        dead: list[web.WebSocketResponse] = []
        delivered = 0
        for ws in list(self._clients):
            try:
                await ws.send_str(payload)
                delivered += 1
            except (ConnectionError, asyncio.TimeoutError):
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
        if delivered > 0:
            await models.mark_tts_reproduzido(self.db, tts_id)
            logger.info("📢 TTS #%d broadcasted to %d client(s): \"%s...\"", tts_id, delivered, message[:60])
        else:
            logger.warning("⚠️ TTS #%d nao entregue a nenhum cliente (todos offline).", tts_id)
