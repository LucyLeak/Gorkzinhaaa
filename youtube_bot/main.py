from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from openai import AsyncOpenAI

from youtube_bot.brains.cerebro_a import CerebroA
from youtube_bot.brains.cerebro_b import CerebroB
from youtube_bot.brains.diretor import Director
from youtube_bot.config import load_settings
from youtube_bot.db import models
from youtube_bot.db.models import ensure_admin_test_user, initialize_schema
from youtube_bot.db.pool import Database
from youtube_bot.fun.giphy import GiphyClient
from youtube_bot.fun.trivia import TriviaGame
from youtube_bot.memory.cleanup import cleanup_on_startup
from youtube_bot.memory.consolidator import MemoryConsolidator
from youtube_bot.memory.vector_store import VectorMemoryStore
from youtube_bot.tts_ws_server import TtsWebSocketServer
from youtube_bot.utils.helpers import (
    extract_youtube_video_id,
    utc_now,
    prepare_chat_message,
    sanitize_for_chat,
)
from youtube_bot.utils.logger import configure_logging
from youtube_bot.validation.validator import Validator
from youtube_bot.youtube.client import (
    YouTubeClient,
    YouTubeComment,
    YouTubeLiveEndedError,
    YouTubeQuotaExceededError,
)
from youtube_bot.youtube.live import LiveChatClient, YouTubeLiveMessage
from youtube_bot.youtube.live import LiveChatStopReason
from youtube_bot.youtube.schedule import LiveDiscoverySchedule, ScheduledLiveMonitor
from youtube_bot.youtube.quota import (
    QuotaGuardTriggered,
    QuotaTracker,
    quota_exceeded_payload,
)

logger = logging.getLogger(__name__)

# Intervalo para verificar novas lives no canal (em segundos)
LIVE_DISCOVERY_INTERVAL = 60
TTS_CLEANUP_INTERVAL_SECONDS = 600


async def _tts_cleanup_loop(db: Database, settings) -> None:
    while True:
        await asyncio.sleep(TTS_CLEANUP_INTERVAL_SECONDS)
        started = time.monotonic()
        try:
            result = await models.cleanup_old_tts(
                db,
                settings.tts_retention_hours,
                settings.tts_api_retention_hours,
            )
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            logger.info("TTS cleanup: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("TTS cleanup failed; continuing next cycle.", exc_info=True)


def _normalize_youtube_handle(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def _is_bot_author(
    author_channel_id: str,
    author_name: str,
    bot_channel_id: str,
    bot_handle: str,
) -> bool:
    if bot_channel_id and author_channel_id == bot_channel_id:
        return True
    return bool(
        bot_handle
        and _normalize_youtube_handle(author_name)
        == _normalize_youtube_handle(bot_handle)
    )


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    if settings.dry_run:
        logger.warning("DRY_RUN=true: respostas serao logadas, mas nao postadas.")

    db = Database(settings.database_url)
    await db.connect()
    await initialize_schema(db)
    await ensure_admin_test_user(
        db, settings.admin_test_user_id, settings.admin_test_username
    )
    await ensure_admin_test_user(db, 999999998, "[api]")

    openai_client = (
        AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        if settings.openai_api_key
        else None
    )
    vector_store = VectorMemoryStore(
        db=db,
        openai_client=openai_client,
        embedding_model=settings.openai_embedding_model,
        embedding_dimensions=settings.openai_embedding_dimensions,
    )
    validator = Validator(
        forbidden_words=settings.forbidden_words,
        coherence_threshold=settings.coherence_threshold,
        vector_store=vector_store,
    )

    brain_a = CerebroA(
        settings.openai_chat_model, openai_client, settings.openai_json_mode
    )
    brain_b = CerebroB(
        settings.openai_chat_model, openai_client, settings.openai_json_mode
    )
    trivia = TriviaGame(Path("data/trivia_questions.json"))
    giphy = GiphyClient(settings.giphy_api_key)
    consolidator = MemoryConsolidator(
        db=db,
        vector_store=vector_store,
        openai_client=openai_client,
        chat_model=settings.openai_chat_model,
    )
    director = Director(
        brain_a=brain_a,
        brain_b=brain_b,
        db=db,
        validator=validator,
        vector_store=vector_store,
        settings=settings,
        trivia=trivia,
        giphy=giphy,
        consolidator=consolidator,
    )
    quota_tracker = QuotaTracker(db, settings.youtube_quota_safety_margin)
    youtube_client = YouTubeClient(settings, db=db, quota_tracker=quota_tracker)
    live_client = LiveChatClient(youtube_client)
    logger.info(
        "Live chat polling interval: %ss (YouTube recommends %ss).",
        settings.youtube_live_poll_interval_seconds,
        5,
    )

    # Limpeza unica de dados antigos na inicializacao
    await cleanup_on_startup(db, settings.memory_retention_days)

    # ── TTS WebSocket server (embedded, reachable publicly) ──────
    tts_ws = TtsWebSocketServer(
        db=db,
        host=settings.tts_ws_host,
        port=settings.tts_ws_port,
        poll_interval=2.0,
        settings=settings,
        quota_tracker=quota_tracker,
    )
    await tts_ws.start()
    tts_cleanup_task = asyncio.create_task(_tts_cleanup_loop(db, settings))
    logger.info(
        "TTS cleanup task started (interval=%ss, retention=%sh, api_retention=%sh).",
        TTS_CLEANUP_INTERVAL_SECONDS,
        settings.tts_retention_hours,
        settings.tts_api_retention_hours,
    )

    # ── Resolver channel ID a partir do @handle ──────────────────────
    live_video_id: str | None = None
    channel_mode = bool(settings.youtube_channel_id or settings.youtube_channel_handle)
    scheduled_channel_mode = bool(settings.youtube_live_schedule_enabled and channel_mode)

    live_url = settings.youtube_live_url.strip()
    if live_url:
        live_video_id = extract_youtube_video_id(live_url)
        if not live_video_id:
            logger.warning(
                "YOUTUBE_LIVE_URL=%r nao e valido; ignorando-o.",
                live_url,
            )
        else:
            logger.info("Modo live direta ativo: video_id=%s", live_video_id)
            tts_ws.set_live_video_id(live_video_id)

    scheduled_channel_mode = bool(scheduled_channel_mode and not live_video_id)

    channel_id: str | None = settings.youtube_channel_id or None
    if settings.youtube_channel_handle and not live_video_id and not scheduled_channel_mode:
        channel_id = await youtube_client.resolve_channel_id(
            settings.youtube_channel_handle
        )
        if channel_id:
            logger.info(
                "Modo canal ativo: monitorando lives de %s (channel_id=%s)",
                settings.youtube_channel_handle,
                channel_id,
            )
        else:
            logger.error(
                "Nao foi possivel resolver o handle @%s. Verifique YOUTUBE_CHANNEL_HANDLE.",
                settings.youtube_channel_handle,
            )

    # ── Fallback: video IDs fixos ────────────────────────────────────
    last_seen_by_video = {vid: utc_now() for vid in settings.youtube_video_ids}

    # ── Controle de lives ja conhecidas ──────────────────────────────
    known_live_ids: set[str] = set()
    live_discovery_enabled = True

    if (
        not live_video_id
        and not channel_id
        and not settings.youtube_video_ids
        and not scheduled_channel_mode
    ):
        logger.warning(
            "Nenhuma fonte do YouTube configurada. "
            "O TTS WebSocket server continuara rodando em ws://%s:%s/ws. "
            "Para ativar o YouTube, configure YOUTUBE_LIVE_URL, "
            "YOUTUBE_CHANNEL_HANDLE ou YOUTUBE_VIDEO_IDS.",
            settings.tts_ws_host,
            settings.tts_ws_port,
        )
        # Keep the bot alive with just the WS server running
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        tts_cleanup_task.cancel()
        await asyncio.gather(tts_cleanup_task, return_exceptions=True)
        return

    scheduled_monitor_task: asyncio.Task[None] | None = None
    scheduled_monitor: ScheduledLiveMonitor | None = None
    if channel_mode:
        scheduled_monitor = ScheduledLiveMonitor(
            youtube_client=youtube_client,
            schedule=LiveDiscoverySchedule.from_settings(settings),
            start_live_chat=lambda video_id: start_scheduled_live_chat(
                youtube_client=youtube_client,
                live_client=live_client,
                director=director,
                video_id=video_id,
                bot_channel_id=settings.youtube_bot_channel_id,
                bot_handle=settings.youtube_bot_handle,
                connect_message=settings.youtube_live_connect_message,
                message_counter=lambda: setattr(
                    scheduled_monitor,
                    "message_count",
                    scheduled_monitor.message_count + 1,
                ),
            ),
            channel_id=settings.youtube_channel_id,
            channel_handle=settings.youtube_channel_handle,
        )
        if scheduled_channel_mode:
            scheduled_monitor_task = asyncio.create_task(scheduled_monitor.run())
            logger.info("Modo de agenda de lives ativado.")

        async def force_live_check() -> dict:
            logger.info("[MANUAL] Force live check requested by admin")
            if scheduled_monitor._active_task is not None and not scheduled_monitor._active_task.done():
                return {"ok": True, "live_found": True, "already_connected": True}
            try:
                found = await scheduled_monitor.force_check()
            except (YouTubeQuotaExceededError, QuotaGuardTriggered):
                return quota_exceeded_payload()
            if not found:
                return {
                    "ok": True,
                    "live_found": False,
                    "message": f"Nenhuma live ativa encontrada no canal {settings.youtube_channel_handle or settings.youtube_channel_id}.",
                }
            return {
                "ok": True,
                "live_found": True,
                "already_connected": False,
                "video_id": scheduled_monitor.detected_video_id,
                "live_chat_id": scheduled_monitor.live_chat_id,
                "title": scheduled_monitor.live_title or scheduled_monitor.detected_video_id,
            }

        async def disconnect_live() -> dict:
            status = await quota_tracker.status()
            if status["remaining"] <= 0:
                return quota_exceeded_payload()
            disconnected = await scheduled_monitor.disconnect()
            return {
                "ok": True,
                "disconnected": disconnected,
                **({} if disconnected else {"message": "Nenhuma live conectada."}),
            }

        def live_status() -> dict:
            return scheduled_monitor.status()

        tts_ws.set_live_controls(force_live_check, disconnect_live, live_status)

        if scheduled_channel_mode and settings.youtube_live_recovery_grace_minutes > 0:
            now_local = scheduled_monitor._now()
            window_end = scheduled_monitor.schedule.window_end(now_local)
            if (
                window_end <= now_local
                <= window_end + timedelta(minutes=settings.youtube_live_recovery_grace_minutes)
            ):
                asyncio.create_task(force_live_check())

    quota_pause_until: float = 0  # timestamp until which to skip YouTube API calls

    try:
        while True:
            now = asyncio.get_event_loop().time()

            # Skip YouTube API calls if quota is exhausted
            if now < quota_pause_until:
                await asyncio.sleep(settings.poll_interval_seconds)
                continue

            # 1. Conectar na live direta ou descobrir novas lives do canal
            try:
                if live_video_id and live_video_id not in known_live_ids:
                    await connect_to_live_video(
                        youtube_client=youtube_client,
                        live_client=live_client,
                        director=director,
                        video_id=live_video_id,
                        bot_channel_id=settings.youtube_bot_channel_id,
                        bot_handle=settings.youtube_bot_handle,
                        connect_message=settings.youtube_live_connect_message,
                        known_live_ids=known_live_ids,
                    )
                elif channel_id and live_discovery_enabled and not scheduled_channel_mode:
                    live_discovery_enabled = await discover_and_connect_lives(
                        youtube_client=youtube_client,
                        live_client=live_client,
                        director=director,
                        channel_id=channel_id,
                        bot_channel_id=settings.youtube_bot_channel_id,
                        bot_handle=settings.youtube_bot_handle,
                        connect_message=settings.youtube_live_connect_message,
                        known_live_ids=known_live_ids,
                    )
            except (YouTubeQuotaExceededError, QuotaGuardTriggered):
                quota_pause_until = now + 3600  # Pause YouTube API for 1 hour
                logger.warning(
                    "Quota do YouTube esgotada. Pausando chamadas a API por 1 hora "
                    "(ate %s UTC). O servidor WebSocket e admin panel continuam ativos.",
                    asyncio.get_event_loop().time() + 3600,
                )

            # 2. Poll de comentarios em videos fixos
            if settings.youtube_video_ids and now >= quota_pause_until:
                try:
                    await poll_video_comments(
                        youtube_client, director,
                        settings.youtube_bot_channel_id,
                        settings.youtube_bot_handle,
                        last_seen_by_video,
                    )
                except (YouTubeQuotaExceededError, QuotaGuardTriggered):
                    quota_pause_until = now + 3600
                    logger.warning("Quota do YouTube esgotada nos comentarios. Pausando por 1 hora.")

            await asyncio.sleep(settings.poll_interval_seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bot encerrado pelo usuario (Ctrl+C).")
    finally:
        if scheduled_monitor is not None:
            await scheduled_monitor.stop()
        if scheduled_monitor_task is not None:
            scheduled_monitor_task.cancel()
            await asyncio.gather(scheduled_monitor_task, return_exceptions=True)
        tts_cleanup_task.cancel()
        await asyncio.gather(tts_cleanup_task, return_exceptions=True)
        await tts_ws.stop()
        await db.close()


# ── Live Discovery ────────────────────────────────────────────────────────

async def connect_to_live_video(
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    video_id: str,
    bot_channel_id: str,
    bot_handle: str,
    connect_message: str,
    known_live_ids: set[str],
) -> None:
    """Conecta diretamente a uma live conhecida por URL/video_id."""
    try:
        live_chat_id = await youtube_client.get_active_live_chat_id(video_id)
    except (YouTubeQuotaExceededError, QuotaGuardTriggered):
        raise  # Propagate to main loop for global quota pause
    except Exception as exc:
        logger.warning("Ainda nao foi possivel conectar na live %s: %s", video_id, exc)
        return

    known_live_ids.add(video_id)
    logger.info("Conectando diretamente na live %s (chat_id=%s)", video_id, live_chat_id)
    asyncio.create_task(
        poll_live_chat(
            youtube_client=youtube_client,
            live_client=live_client,
            director=director,
            live_chat_id=live_chat_id,
            video_id=video_id,
            bot_channel_id=bot_channel_id,
            bot_handle=bot_handle,
            connect_message=connect_message,
        )
    )


async def start_scheduled_live_chat(
    *,
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    video_id: str,
    bot_channel_id: str,
    bot_handle: str,
    connect_message: str,
    message_counter: Callable[[], None] | None = None,
) -> asyncio.Task[LiveChatStopReason]:
    live_chat_id = await youtube_client.get_active_live_chat_id(video_id)
    task = asyncio.create_task(
        poll_live_chat(
            youtube_client=youtube_client,
            live_client=live_client,
            director=director,
            live_chat_id=live_chat_id,
            video_id=video_id,
            bot_channel_id=bot_channel_id,
            bot_handle=bot_handle,
            connect_message=connect_message,
            message_counter=message_counter,
        )
    )
    task.live_chat_id = live_chat_id
    return task


async def discover_and_connect_lives(
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    channel_id: str,
    bot_channel_id: str,
    bot_handle: str,
    connect_message: str,
    known_live_ids: set[str],
) -> bool:
    """Busca lives ativas no canal e conecta nas que ainda nao foram vistas."""
    try:
        lives = await youtube_client.get_active_lives(channel_id)
    except (YouTubeQuotaExceededError, QuotaGuardTriggered):
        raise  # Propagate to main loop for global quota pause
    except ssl.SSLError:
        logger.warning("Erro SSL ao buscar lives (conexao ja resetada internamente).")
        return True
    except Exception:
        logger.exception("Falha ao buscar lives ativas do canal %s.", channel_id)
        return True

    for live in lives:
        live_id = live["video_id"]
        if live_id in known_live_ids:
            continue

        logger.info(
            "Nova live detectada: %s (chat_id=%s)",
            live["title"],
            live["live_chat_id"],
        )
        known_live_ids.add(live_id)

        # Conecta ao chat da live em background
        asyncio.create_task(
            poll_live_chat(
                youtube_client=youtube_client,
                live_client=live_client,
                director=director,
                live_chat_id=live["live_chat_id"],
                video_id=live_id,
                bot_channel_id=bot_channel_id,
                bot_handle=bot_handle,
                connect_message=connect_message,
            )
        )

    return True


async def poll_live_chat(
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    live_chat_id: str,
    video_id: str,
    bot_channel_id: str,
    bot_handle: str,
    connect_message: str,
    message_counter: Callable[[], None] | None = None,
) -> LiveChatStopReason:
    """Loop de polling do chat ao vivo de uma live especifica."""
    logger.info("Conectado ao chat ao vivo: %s (video=%s)", live_chat_id, video_id)
    try:
        quota = await youtube_client.quota_tracker.status() if youtube_client.quota_tracker else None
        if quota:
            logger.info(
                "Quota: %s/%s used (%s remaining) after connecting to live %s",
                quota["used"], quota["limit"], quota["remaining"], video_id,
            )
    except Exception:
        logger.debug("Nao foi possivel registrar a cota apos conectar na live.", exc_info=True)

    # Envia mensagem de "bot ativado" no chat (ignora DRY_RUN)
    if connect_message and connect_message.strip():
        try:
            await live_client.post_message(live_chat_id, connect_message, force=True)
            logger.info("Mensagem de conexao enviada no chat %s.", live_chat_id)
        except Exception:
            logger.exception("Falha ao enviar mensagem de conexao no chat %s.", live_chat_id)
    else:
        logger.info(
            "Connect message disabled (YOUTUBE_LIVE_CONNECT_MESSAGE empty); "
            "skipping post to save 50 quota units."
        )

    page_token: str | None = None
    consecutive_errors = 0
    first_poll = True  # ignora mensagens antigas na primeira chamada

    while True:
        try:
            messages, page_token, poll_interval_ms = await live_client.get_messages(
                live_chat_id, page_token
            )
            consecutive_errors = 0

            if first_poll:
                # Primeira poll: apenas obtem o page_token, ignora mensagens antigas
                first_poll = False
                logger.info(
                    "Chat %s: primeira poll concluida, %d mensagens antigas ignoradas.",
                    live_chat_id,
                    len(messages),
                )
            else:
                for msg in messages:
                    if message_counter is not None:
                        message_counter()
                    if _is_bot_author(
                        msg.author_channel_id,
                        msg.author_name,
                        bot_channel_id,
                        bot_handle,
                    ):
                        continue
                    await process_live_message(live_client, director, msg, live_chat_id)

            # YouTube recomenda esperar o pollingIntervalMillis
            configured = youtube_client.settings.youtube_live_poll_interval_seconds
            await asyncio.sleep(max(poll_interval_ms / 1000.0, configured))

        except (YouTubeQuotaExceededError, QuotaGuardTriggered):
            logger.warning(
                "Cota da API do YouTube esgotada no chat %s. Pausando live chat.",
                live_chat_id,
            )
            return LiveChatStopReason.QUOTA_EXCEEDED
        except YouTubeLiveEndedError:
            logger.info("A live %s terminou.", video_id)
            return LiveChatStopReason.ENDED
        except ssl.SSLError:
            consecutive_errors += 1
            logger.warning(
                "Erro SSL no chat %s (tentativa %s, conexao ja resetada internamente).",
                live_chat_id,
                consecutive_errors,
            )
            if consecutive_errors >= 5:
                logger.warning(
                    "Muitos erros SSL no chat %s. Desconectando.",
                    live_chat_id,
                )
                return LiveChatStopReason.CONNECTION_ERROR
            await asyncio.sleep(10)
        except Exception:
            consecutive_errors += 1
            logger.exception(
                "Erro no chat ao vivo %s (tentativa %s).",
                live_chat_id,
                consecutive_errors,
            )
            if consecutive_errors >= 5:
                logger.warning(
                    "Muitos erros no chat %s. Live pode ter terminado. Desconectando.",
                    live_chat_id,
                )
                return LiveChatStopReason.CONNECTION_ERROR
            await asyncio.sleep(10)


async def process_live_message(
    live_client: LiveChatClient,
    director: Director,
    message: YouTubeLiveMessage,
    live_chat_id: str,
) -> None:
    """Processa uma mensagem do chat ao vivo e responde."""
    try:
        if not message.author_channel_id:
            logger.warning("Mensagem live sem author_channel_id; usando nome como fallback.")
        reply = await director.decide_and_respond(
            user_message=message.text,
            user_youtube_id=message.author_channel_id or message.author_name,
            display_name=message.author_name,
            message_type="live",
            author_channel_id=message.author_channel_id or None,
        )
        logger.info("Raw AI reply: %r", reply.text)
        thought, message_text = prepare_chat_message(
            reply.text,
            allow_plain_text=reply.brain_name not in {"cerebro_a", "cerebro_b"},
        )
        if thought:
            logger.info("Pensamento do bot: %s", thought)

        message_text = sanitize_for_chat(message_text)
        if thought and thought.strip() and thought.casefold() in message_text.casefold():
            logger.error("Pensamento da IA vazou para a mensagem; postagem bloqueada.")
            return
        if reply.text.lstrip().startswith("{") and "thought" in reply.text.casefold() and not message_text:
            logger.error("Resposta JSON contem thought, mas message esta vazia; postagem bloqueada.")
            return
        if not message_text:
            logger.warning(
                "A resposta do bot ficou vazia apos remover o pensamento. Nao sera enviada."
            )
            return

        await live_client.post_message(live_chat_id, message_text)
        logger.info(
            "Live %s: respondido com %s.",
            live_chat_id,
            reply.brain_name,
        )
    except Exception:
        logger.exception("Falha ao processar mensagem da live %s.", live_chat_id)


# ── Video Comments (modo legado) ──────────────────────────────────────────

async def poll_video_comments(
    youtube_client: YouTubeClient,
    director: Director,
    bot_channel_id: str,
    bot_handle: str,
    last_seen_by_video: dict,
) -> None:
    for video_id, last_seen in list(last_seen_by_video.items()):
        try:
            comments = await youtube_client.get_new_comments(video_id, last_seen)
        except (YouTubeQuotaExceededError, QuotaGuardTriggered):
            raise  # Propagate to main loop for global quota pause
        except Exception:
            logger.exception("Falha ao buscar comentarios do video %s.", video_id)
            continue

        if not comments:  # No new comments at all
            continue

        # Always update last_seen to the newest comment to avoid reprocessing
        newest = max(comment.published_at for comment in comments)
        last_seen_by_video[video_id] = max(last_seen, newest)

        # Filter out bot's own comments
        user_comments = [
            c for c in comments
            if not _is_bot_author(
                c.author_channel_id,
                c.author_name,
                bot_channel_id,
                bot_handle,
            )
        ]

        if not user_comments:
            logger.info("Video %s: %d novos comentarios encontrados, mas nenhum de usuario.", video_id, len(comments))
            continue

        # Take the 5 most recent user comments
        # Comments are sorted oldest to newest, so we take the tail.
        candidates = user_comments[-5:]
        comment_to_reply = random.choice(candidates)

        logger.info(
            "Video %s: %d novos comentarios de usuario. Escolhido aleatoriamente o comentario %s para responder (de %d candidatos).",
            video_id,
            len(user_comments),
            comment_to_reply.comment_id,
            len(candidates),
        )

        # Process only the selected comment
        await process_comment(youtube_client, director, comment_to_reply)


async def process_comment(
    youtube_client: YouTubeClient,
    director: Director,
    comment: YouTubeComment,
) -> None:
    try:
        if not comment.author_channel_id:
            logger.warning("Comentário sem author_channel_id; usando nome como fallback.")
        reply = await director.decide_and_respond(
            user_message=comment.text,
            user_youtube_id=comment.author_channel_id or comment.author_name,
            display_name=comment.author_name,
            message_type="comment",
            author_channel_id=comment.author_channel_id or None,
        )
        thought, message_text = prepare_chat_message(
            reply.text,
            allow_plain_text=reply.brain_name not in {"cerebro_a", "cerebro_b"},
        )
        if thought:
            logger.info("Pensamento do bot: %s", thought)

        message_text = sanitize_for_chat(message_text)
        if thought and thought.strip() and thought.casefold() in message_text.casefold():
            logger.error("Pensamento da IA vazou para o comentario; postagem bloqueada.")
            return
        if reply.text.lstrip().startswith("{") and "thought" in reply.text.casefold() and not message_text:
            logger.error("Resposta JSON contem thought, mas message esta vazia; postagem bloqueada.")
            return
        if not message_text:
            logger.warning(
                "A resposta do bot ficou vazia apos remover o pensamento. Nao sera enviada."
            )
            return
        await youtube_client.post_reply(comment.comment_id, message_text)
        logger.info(
            "Comentario %s respondido com %s.",
            comment.comment_id,
            reply.brain_name,
        )
    except Exception:
        logger.exception("Falha ao processar comentario %s.", comment.comment_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # saida limpa, sem traceback
