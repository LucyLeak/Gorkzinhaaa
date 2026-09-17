from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.admin_panel import AdminPanel

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
        self._admin_clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._admin_poll_task: asyncio.Task[None] | None = None
        self._admin_queue_signature: str | None = None
        self._site: web.TCPSite | None = None

        # Ensure the TTS audio directory exists (needed for static route and TTS generation)
        self._audio_dir = Path(settings.tts_output_dir if settings else "data/tts_audio")
        self._audio_dir.mkdir(parents=True, exist_ok=True)

        # Routes
        self._app.router.add_get("/", self._handle_health)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/admin/ws", self._handle_admin_websocket)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/pending-tts", self._handle_pending_tts)
        # Serve local TTS audio files as fallback when Catbox is down
        self._app.router.add_static("/audio/", self._audio_dir, show_index=False)

        # Admin panel (TTS approval, cleanup, terminal)
        self._admin = AdminPanel(
            db=self.db,
            settings=self.settings,
            on_tts_changed=self._broadcast_admin_queue,
        )
        self._admin.register_routes(self._app)

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
        self._admin_poll_task = asyncio.create_task(self._admin_queue_loop())

    async def stop(self) -> None:
        """Stop the poller and shut down the HTTP server."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._admin_poll_task:
            self._admin_poll_task.cancel()
            try:
                await self._admin_poll_task
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

    async def _handle_admin_websocket(self, request: web.Request) -> web.WebSocketResponse:
        if not self._admin._check_auth(request):
            return web.Response(status=401, text="Unauthorized")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._admin_clients.add(ws)
        try:
            await self._send_admin_queue(ws)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await self._send_json(ws, {"type": "tts_test_progress", "message": "Payload inválido.", "error": True})
                    continue
                if payload.get("type") == "tts_test":
                    await self._run_tts_test(ws, payload)
        finally:
            self._admin_clients.discard(ws)
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

    async def _admin_queue_loop(self) -> None:
        while True:
            try:
                await self._broadcast_admin_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Admin TTS queue update failed.")
            await asyncio.sleep(0.5)

    async def _send_json(self, ws: web.WebSocketResponse, payload: dict) -> None:
        await ws.send_str(json.dumps(payload, ensure_ascii=False, default=str))

    async def _send_admin_queue(self, ws: web.WebSocketResponse) -> None:
        items = await self._admin.get_tts_queue_items()
        await self._send_json(ws, {"type": "tts_queue", "items": items})

    async def _broadcast_admin_queue(self) -> None:
        if not self._admin_clients:
            return
        items = await self._admin.get_tts_queue_items()
        signature = json.dumps(items, sort_keys=True, ensure_ascii=False, default=str)
        if signature == self._admin_queue_signature:
            return
        self._admin_queue_signature = signature
        payload = {"type": "tts_queue", "items": items}
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._admin_clients):
            try:
                await self._send_json(ws, payload)
            except (ConnectionError, asyncio.TimeoutError):
                dead.append(ws)
        for ws in dead:
            self._admin_clients.discard(ws)

    async def _run_tts_test(self, ws: web.WebSocketResponse, payload: dict) -> None:
        if self.settings is None:
            await self._send_json(ws, {"type": "tts_test_progress", "message": "Configuração TTS indisponível.", "error": True})
            return

        from youtube_bot.fun.tts import generate_tts, sanitize_tts_text, upload_tts_audio

        text = sanitize_tts_text(str(payload.get("text") or ""))
        if not text:
            await self._send_json(ws, {"type": "tts_test_progress", "message": "O texto ficou vazio após a limpeza.", "error": True})
            return

        provider = str(payload.get("provider") or self.settings.tts_provider).strip().lower()
        voice = str(payload.get("voice") or self.settings.tts_voice).strip()
        test_settings = replace(
            self.settings,
            tts_provider=provider,
            tts_voice=voice,
            elevenlabs_voice_id=str(payload.get("elevenlabs_voice_id") or self.settings.elevenlabs_voice_id),
            elevenlabs_model_id=str(payload.get("elevenlabs_model_id") or self.settings.elevenlabs_model_id),
            elevenlabs_output_format=str(payload.get("elevenlabs_output_format") or self.settings.elevenlabs_output_format),
        )
        await self._send_json(ws, {"type": "tts_test_progress", "message": "Iniciando síntese..."})
        try:
            await self._send_json(ws, {"type": "tts_test_progress", "message": "Sintetizando..."})
            audio_path = await generate_tts(text, test_settings, self.db, user_id=0, voice=voice)
            await self._send_json(ws, {"type": "tts_test_progress", "message": "Enviando áudio..."})
            audio_url = await upload_tts_audio(audio_path, test_settings)
            if not audio_url:
                raise RuntimeError("Não foi possível obter uma URL pública para o áudio.")
            await self._send_json(ws, {
                "type": "tts_test_result",
                "ok": True,
                "audio_url": audio_url,
                "provider": provider,
                "voice": voice,
            })
        except Exception as exc:
            logger.exception("Admin TTS test failed.")
            await self._send_json(ws, {"type": "tts_test_result", "ok": False, "error": str(exc)})

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

    # ── HTTP queue endpoint ─────────────────────────────────────────

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
