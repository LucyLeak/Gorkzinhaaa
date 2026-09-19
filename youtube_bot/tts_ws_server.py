from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.admin_panel import AdminPanel
from youtube_bot.api_docs import API_DOCS_HTML as PUBLIC_API_DOCS_HTML

if TYPE_CHECKING:
    from youtube_bot.config import Settings

logger = logging.getLogger(__name__)
API_TTS_USER_ID = 999999998


def _isoformat_or_none(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


API_DOCS_HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>API Gorkzinhaaa</title>
<style>:root{color-scheme:dark;--bg:#0b1020;--card:#141d35;--line:#29385c;--text:#e8eefc;--muted:#9eadd0;--accent:#73a7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui;line-height:1.55}
main{max-width:980px;margin:auto;padding:40px 20px}h1,h2{color:var(--accent)}section{background:var(--card);padding:20px;border:1px solid var(--line);border-radius:14px;margin:18px 0}
code,pre{background:#080d19;border-radius:8px;padding:3px 6px}pre{padding:14px;overflow:auto}.endpoint{color:#91d7ac;font-family:monospace}button{background:var(--accent);border:0;border-radius:7px;padding:7px 12px;cursor:pointer}
.try{display:flex;gap:8px;flex-wrap:wrap}input{background:#080d19;color:var(--text);border:1px solid var(--line);padding:8px;border-radius:7px;flex:1}</style></head>
<body><main><h1>Gorkzinhaaa API</h1><p>API pública de síntese de voz. Base URL: <code id="base"></code></p>
<section><h2>Autenticacao e escopos</h2><p>Envie <code>Authorization: Bearer &lt;API_KEY&gt;</code>. Chaves sao criadas no painel administrativo e exibidas uma unica vez.</p><p>Escopos: <code>tts:generate</code>, <code>tts:subscribe</code>, <code>status:read</code>. Limite de WS: 5 conexoes por chave; espera sincronica usa o limite configurado.</p></section>
<section><h2>Endpoints</h2><p class=endpoint>POST /api/v1/tts</p><p>JSON: <code>{"text":"Olá","provider":"gtts","voice":"pt"}</code>. Use <code>?wait=true</code> ou <code>Prefer: wait=15</code>. Retorna 202 para polling ou 200 ao concluir.</p>
<pre>curl -X POST "$BASE/api/v1/tts" -H "Authorization: Bearer KEY" -H "Content-Type: application/json" -d '{"text":"Olá"}'
curl "$BASE/api/v1/tts/123" -H "Authorization: Bearer KEY"
curl "$BASE/api/v1/status" -H "Authorization: Bearer KEY"
curl "$BASE/api/v1/health"</pre>
<p class=endpoint>GET /api/v1/tts/{id} · GET /api/v1/status · GET /api/v1/health</p>
<p>Erros usam <code>{"error":{"code":"...","message":"..."}}</code>. CORS é controlado por <code>API_ALLOWED_ORIGINS</code>.</p></section>
<section><h2>WebSocket</h2><p><code>wss://host/api/v1/ws?key=KEY</code> exige <code>tts:subscribe</code>. Primeiro evento: <code>{"type":"hello","client":"nome"}</code>. Conclusões: <code>{"type":"tts","id":1,"audio":"...","username":"...","message":"...","created_at":"..."}</code>.</section>
<section><h2>Teste rápido</h2><div class=try><button onclick="fetch('/api/v1/health').then(r=>r.json()).then(x=>out.textContent=JSON.stringify(x,null,2))">Testar health</button><button onclick="navigator.clipboard.writeText(location.origin+'/api/v1/health')">Copiar URL</button></div><pre id=out></pre></section>
<p><a href="/admin">Painel administrativo</a></p></main><script>base.textContent=location.origin;const BASE=location.origin;</script></body></html>"""


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
        self._started_at = time.time()
        self._last_live_video_id: str | None = None
        self._live_connected = False
        self._api_waiters = asyncio.Semaphore(
            max(1, getattr(settings, "api_tts_max_concurrent", 5))
        )
        self._api_clients: dict[web.WebSocketResponse, dict] = {}
        self._api_tasks: set[asyncio.Task] = set()
        self._api_connection_counts: dict[int, int] = {}
        self._api_rate_limits: dict[tuple[int, str], list[float]] = {}
        self._api_rate_limit_prune_at = 0.0

        # Ensure the TTS audio directory exists (needed for static route and TTS generation)
        self._audio_dir = Path(settings.tts_output_dir if settings else "data/tts_audio")
        self._audio_dir.mkdir(parents=True, exist_ok=True)

        # Routes
        self._app.router.add_get("/", self._handle_health)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/admin/ws", self._handle_admin_websocket)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/pending-tts", self._handle_pending_tts)
        self._app.router.add_get("/docs", self._handle_docs)
        self._app.router.add_get("/api/docs", self._handle_docs)
        self._app.router.add_route("*", "/api/v1/health", self._handle_api_health)
        self._app.router.add_route("*", "/api/v1/tts", self._handle_api_tts)
        self._app.router.add_route("*", "/api/v1/tts/{id}", self._handle_api_tts_item)
        self._app.router.add_route("*", "/api/v1/status", self._handle_api_status)
        self._app.router.add_get("/api/v1/ws", self._handle_api_websocket)
        # Serve local TTS audio files as fallback when Catbox is down
        self._app.router.add_static("/audio/", self._audio_dir, show_index=False)

        # Admin panel (TTS approval, cleanup, terminal)
        self._admin = AdminPanel(
            db=self.db,
            settings=self.settings,
            on_tts_changed=self._broadcast_admin_queue,
            on_api_client_revoked=self._close_api_client_connections,
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

    def set_live_video_id(self, video_id: str | None) -> None:
        self._last_live_video_id = video_id
        self._live_connected = bool(video_id)

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
        for task in list(self._api_tasks):
            task.cancel()
        await asyncio.gather(*self._api_tasks, return_exceptions=True)
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
        return web.json_response({
            "status": "ok", "clients": len(self._clients), "docs": "/docs"
        })

    def _cors_headers(self, request: web.Request) -> dict[str, str]:
        origins = getattr(self.settings, "api_allowed_origins", ()) if self.settings else ()
        origin = request.headers.get("Origin")
        headers: dict[str, str] = {}
        if origin and (origin in origins):
            headers.update({
                "Access-Control-Allow-Origin": origin,
                "Vary": "Origin",
            })
        return headers

    def _api_response(self, request: web.Request, data: dict, status: int = 200) -> web.Response:
        return web.json_response(data, status=status, headers=self._cors_headers(request))

    def _api_error(self, request: web.Request, code: str, message: str, status: int) -> web.Response:
        return self._api_response(request, {"error": {"code": code, "message": message}}, status)

    async def _api_options(
        self, request: web.Request, methods: str = "GET, POST, OPTIONS"
    ) -> web.Response:
        headers = self._cors_headers(request)
        headers.update({
            "Access-Control-Allow-Methods": methods,
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Prefer",
            "Access-Control-Max-Age": "86400",
        })
        return web.Response(status=204, headers=headers)

    async def _find_api_client(self, key: str) -> dict | None:
        if not key:
            return None
        rows = await self.db.fetch(
            "SELECT * FROM api_clients WHERE key_prefix = $1 "
            "AND revoked_at IS NULL ORDER BY id",
            key[:12],
        )
        for row in rows:
            value = str(row["key_hash"])
            try:
                scheme, salt, digest = value.split("$", 2)
                candidate = hashlib.sha256((salt + key).encode()).hexdigest()
                if scheme == "sha256" and hmac.compare_digest(candidate, digest):
                    return dict(row)
            except ValueError:
                continue
        return None

    async def _api_auth(self, request: web.Request, scope: str | None = None) -> dict | None:
        header = request.headers.get("Authorization", "")
        key = header[7:].strip() if header.lower().startswith("bearer ") else request.query.get("key", "")
        client = await self._find_api_client(key)
        if client and (scope is None or scope in (client.get("scopes") or [])):
            return client
        return None

    def _api_rate_limit(
        self,
        request: web.Request,
        client: dict,
        bucket: str,
        limit: int,
    ) -> web.Response | None:
        now = time.monotonic()
        if now >= self._api_rate_limit_prune_at:
            for stored_key, stored_timestamps in list(self._api_rate_limits.items()):
                active = [timestamp for timestamp in stored_timestamps if now - timestamp < 60]
                if active:
                    self._api_rate_limits[stored_key] = active
                else:
                    self._api_rate_limits.pop(stored_key, None)
            self._api_rate_limit_prune_at = now + 60
        key = (int(client["id"]), bucket)
        timestamps = [
            timestamp
            for timestamp in self._api_rate_limits.get(key, [])
            if now - timestamp < 60
        ]
        if len(timestamps) >= limit:
            retry_after = max(1, int(60 - (now - timestamps[0]) + 0.999))
            response = self._api_error(
                request,
                "rate_limited",
                "Limite de requisições excedido.",
                429,
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        timestamps.append(now)
        self._api_rate_limits[key] = timestamps
        return None

    async def _handle_api_health(self, request: web.Request) -> web.Response:
        if request.method == "OPTIONS":
            return await self._api_options(request, "GET, OPTIONS")
        if request.method != "GET":
            return self._api_error(request, "method_not_allowed", "Use GET.", 405)
        return self._api_response(request, {"ok": True})

    async def _handle_api_status(self, request: web.Request) -> web.Response:
        if request.method == "OPTIONS":
            return await self._api_options(request)
        if request.method != "GET":
            return self._api_error(request, "method_not_allowed", "Use GET.", 405)
        client = await self._api_auth(request, "status:read")
        if not client:
            return self._api_error(request, "unauthorized", "API key ou escopo inválido.", 401)
        rate_limit = self._api_rate_limit(request, client, "status", 30)
        if rate_limit is not None:
            return rate_limit
        pending = await self.db.fetchval(
            "SELECT count(*) FROM tts_solicitacoes WHERE status IN ('pendente','processando')"
        )
        return self._api_response(request, {
            "uptime": int(time.time() - self._started_at),
            "live_connected": self._live_connected,
            "pending_tts": int(pending or 0),
            "last_live_video_id": self._last_live_video_id,
        })

    async def _handle_api_tts(self, request: web.Request) -> web.Response:
        if request.method == "OPTIONS":
            return await self._api_options(request)
        client = await self._api_auth(request, "tts:generate")
        if not client:
            return self._api_error(request, "unauthorized", "API key ou escopo inválido.", 401)
        if request.method != "POST":
            return self._api_error(request, "method_not_allowed", "Use POST.", 405)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._api_error(request, "invalid_json", "JSON inválido.", 400)
        from youtube_bot.fun.tts import sanitize_tts_text
        text = sanitize_tts_text(str(body.get("text") or ""))
        if not text:
            return self._api_error(request, "invalid_text", "text é obrigatório.", 400)
        settings = self.settings
        if settings is None:
            return self._api_error(request, "unavailable", "TTS indisponível.", 503)
        provider = str(body.get("provider") or settings.tts_provider)
        voice = str(body.get("voice") or settings.tts_voice)
        wait_requested = request.query.get("wait", "").lower() == "true"
        requested_wait_seconds: int | None = None
        prefer = request.headers.get("Prefer", "")
        if not wait_requested and "wait=" in prefer:
            try:
                requested_wait_seconds = int(
                    prefer.split("wait=", 1)[1].split(",", 1)[0]
                )
                wait_requested = requested_wait_seconds > 0
            except ValueError:
                pass
        if wait_requested and self._api_waiters.locked():
            return self._api_error(request, "busy", "Limite de esperas síncronas atingido.", 429)
        rate_limit = self._api_rate_limit(request, client, "tts_generate", 10)
        if rate_limit is not None:
            return rate_limit
        try:
            row_id = await models.insert_admin_tts_request(
                self.db, API_TTS_USER_ID, str(body.get("text") or text), text,
                status="processando", source="api",
            )
        except Exception:
            logger.exception("Failed to create public TTS request")
            return self._api_error(request, "database_error", "Não foi possível criar a solicitação.", 503)
        task = asyncio.create_task(self._generate_api_tts(row_id, text, provider, voice))
        self._api_tasks.add(task)
        task.add_done_callback(self._api_tasks.discard)
        if wait_requested:
            configured_timeout = max(
                1, getattr(settings, "api_tts_wait_timeout_seconds", 15)
            )
            timeout = min(
                configured_timeout,
                max(1, requested_wait_seconds or configured_timeout),
            )
            async with self._api_waiters:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout)
                except asyncio.TimeoutError:
                    pass
                row = await models.get_tts_request(self.db, row_id)
                if row and row.get("status") in ("concluido", "erro"):
                    return self._api_response(request, self._serialize_tts(row), 200)
        return self._api_response(request, {
            "id": row_id, "status": "processando", "poll_url": f"/api/v1/tts/{row_id}"
        }, 202)

    async def _generate_api_tts(self, row_id: int, text: str, provider: str, voice: str) -> None:
        from dataclasses import replace
        from youtube_bot.fun.tts import generate_tts, upload_tts_audio
        try:
            settings = replace(self.settings, tts_provider=provider, tts_voice=voice)
            path = await generate_tts(text, settings, self.db, API_TTS_USER_ID, voice=voice)
            url = await upload_tts_audio(path, settings)
            if not url:
                raise RuntimeError("Não foi possível publicar o áudio.")
            await models.update_admin_tts_request(self.db, row_id, "concluido", url)
        except asyncio.CancelledError:
            await models.update_admin_tts_request(self.db, row_id, "erro", erro="Solicitação cancelada.")
            raise
        except Exception as exc:
            logger.exception("Public TTS generation failed")
            await models.update_admin_tts_request(self.db, row_id, "erro", erro=str(exc))

    def _serialize_tts(self, row: dict) -> dict:
        return {
            "id": row["id"], "status": row.get("status"), "audio_url": row.get("audio_url"),
            "text": row.get("texto_original"), "error": row.get("erro"),
            "created_at": _isoformat_or_none(row.get("criado_em")),
            "completed_at": _isoformat_or_none(row.get("concluido_em")),
        }

    async def _handle_api_tts_item(self, request: web.Request) -> web.Response:
        if request.method == "OPTIONS":
            return await self._api_options(request)
        if request.method != "GET":
            return self._api_error(request, "method_not_allowed", "Use GET.", 405)
        client = await self._api_auth(request)
        if not client or not ({"tts:generate", "status:read"} & set(client.get("scopes") or [])):
            return self._api_error(request, "unauthorized", "API key ou escopo inválido.", 401)
        rate_limit = self._api_rate_limit(request, client, "tts_item", 60)
        if rate_limit is not None:
            return rate_limit
        try:
            row = await models.get_tts_request(self.db, int(request.match_info["id"]))
        except (ValueError, TypeError):
            row = None
        if not row:
            return self._api_error(request, "not_found", "Solicitação não encontrada.", 404)
        return self._api_response(request, self._serialize_tts(row))

    async def _handle_api_websocket(self, request: web.Request) -> web.StreamResponse:
        client = await self._api_auth(request, "tts:subscribe")
        if not client:
            return self._api_error(request, "unauthorized", "API key ou escopo inválido.", 401)
        client_id = int(client["id"])
        if self._api_connection_counts.get(client_id, 0) >= 5:
            return self._api_error(request, "connection_limit", "Limite de 5 conexões atingido.", 429)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._api_clients[ws] = client
        self._api_connection_counts[client_id] = self._api_connection_counts.get(client_id, 0) + 1
        await ws.send_json({"type": "hello", "client": client["name"]})
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            self._api_clients.pop(ws, None)
            remaining = self._api_connection_counts.get(client_id, 1) - 1
            if remaining > 0:
                self._api_connection_counts[client_id] = remaining
            else:
                self._api_connection_counts.pop(client_id, None)
        return ws

    async def _close_api_client_connections(self, client_id: int) -> None:
        for ws, client in list(self._api_clients.items()):
            if int(client["id"]) == client_id:
                await ws.close(code=1008, message=b"API key revogada")
                self._api_clients.pop(ws, None)

    async def _handle_docs(self, request: web.Request) -> web.Response:
        return web.Response(text=PUBLIC_API_DOCS_HTML, content_type="text/html")

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

        raw_text = str(payload.get("text") or "")
        text = sanitize_tts_text(raw_text)
        if not text:
            await models.insert_admin_tts_request(
                self.db,
                self.settings.admin_test_user_id,
                raw_text,
                text,
                status="erro",
                erro="O texto ficou vazio após a limpeza.",
            )
            await self._send_json(ws, {"type": "tts_test_progress", "message": "O texto ficou vazio após a limpeza.", "error": True})
            return

        provider = str(payload.get("provider") or self.settings.tts_provider).strip().lower()
        voice = str(payload.get("voice") or self.settings.tts_voice).strip()
        model = str(payload.get("model") or self.settings.elevenlabs_model_id).strip()
        output_format = str(
            payload.get("output_format") or self.settings.elevenlabs_output_format
        ).strip()
        test_settings = replace(
            self.settings,
            tts_provider=provider,
            tts_voice=voice,
            elevenlabs_voice_id=str(payload.get("elevenlabs_voice_id") or self.settings.elevenlabs_voice_id),
            elevenlabs_model_id=str(payload.get("elevenlabs_model_id") or self.settings.elevenlabs_model_id),
            elevenlabs_output_format=output_format,
        )
        await self._send_json(ws, {"type": "tts_test_progress", "message": "Iniciando síntese..."})
        try:
            await self._send_json(ws, {"type": "tts_test_progress", "message": "Sintetizando..."})
            audio_path = await generate_tts(
                text,
                test_settings,
                self.db,
                user_id=0,
                voice=voice,
                model=model,
                load_runtime_settings=False,
            )
            await self._send_json(ws, {"type": "tts_test_progress", "message": "Enviando áudio..."})
            audio_url = await upload_tts_audio(audio_path, test_settings)
            if not audio_url:
                raise RuntimeError("Não foi possível obter uma URL pública para o áudio.")
            tts_id = await models.insert_admin_tts_request(
                self.db,
                self.settings.admin_test_user_id,
                raw_text,
                text,
                audio_url=audio_url,
                status="concluido",
            )
            await self._send_json(ws, {
                "type": "tts_test_result",
                "ok": True,
                "audio_url": audio_url,
                "provider": provider,
                "voice": voice,
            })
        except Exception as exc:
            logger.exception("Admin TTS test failed.")
            await models.insert_admin_tts_request(
                self.db,
                self.settings.admin_test_user_id,
                raw_text,
                text,
                status="erro",
                erro=str(exc),
            )
            await self._send_json(ws, {"type": "tts_test_result", "ok": False, "error": str(exc)})

    async def _poll_once(self) -> None:
        if not self._clients and not self._api_clients:
            return  # nobody connected, skip the query

        rows = await models.get_pending_tts(self.db, limit=10)
        for row in rows:
            tts_id = int(row["id"])
            if not await models.claim_tts_broadcast(self.db, tts_id):
                continue
            try:
                await self._broadcast_tts(
                    tts_id=tts_id,
                    username=str(row.get("autor") or ""),
                    message=str(row.get("texto_falado") or ""),
                    audio_url=str(row.get("audio_url") or ""),
                )
            except Exception:
                await models.release_tts_broadcast_claim(self.db, tts_id)
                raise

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
        row = await models.get_tts_request(self.db, tts_id)
        api_payload = {"type": "tts", "id": tts_id, "username": username,
                       "message": message, "audio": audio_url,
                       "created_at": _isoformat_or_none(row.get("criado_em")) if row else None}
        delivered_any = False
        for api_ws in list(self._api_clients):
            try:
                await api_ws.send_json(api_payload)
                delivered_any = True
            except (ConnectionError, asyncio.TimeoutError):
                self._api_clients.pop(api_ws, None)
        payload = json.dumps({
            "id": tts_id,
            "username": username,
            "message": message,
            "type": "url",
            "audio": audio_url,
        })
        dead: list[web.WebSocketResponse] = []
        delivered_clients = 0
        for ws in list(self._clients):
            try:
                await ws.send_str(payload)
                delivered_clients += 1
            except (ConnectionError, asyncio.TimeoutError):
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
        if delivered_clients > 0:
            await models.mark_tts_reproduzido(self.db, tts_id)
            logger.info("📢 TTS #%d broadcasted to %d client(s): \"%s...\"", tts_id, delivered_clients, message[:60])
        elif delivered_any:
            logger.info("📢 TTS #%d broadcasted to API client(s): \"%s...\"", tts_id, message[:60])
        else:
            await models.release_tts_broadcast_claim(self.db, tts_id)
            logger.warning("⚠️ TTS #%d nao entregue a nenhum cliente (todos offline).", tts_id)
