from __future__ import annotations

import asyncio
import logging
import random
import ssl
from pathlib import Path

from openai import AsyncOpenAI

from youtube_bot.brains.cerebro_a import CerebroA
from youtube_bot.brains.cerebro_b import CerebroB
from youtube_bot.brains.diretor import Director
from youtube_bot.config import load_settings
from youtube_bot.db.models import initialize_schema
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
    parse_thinking_response,
)
from youtube_bot.utils.logger import configure_logging
from youtube_bot.validation.validator import Validator
from youtube_bot.youtube.client import (
    YouTubeClient,
    YouTubeComment,
    YouTubeQuotaExceededError,
)
from youtube_bot.youtube.live import LiveChatClient, YouTubeLiveMessage

logger = logging.getLogger(__name__)

# Intervalo para verificar novas lives no canal (em segundos)
LIVE_DISCOVERY_INTERVAL = 60


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    if settings.dry_run:
        logger.warning("DRY_RUN=true: respostas serao logadas, mas nao postadas.")

    db = Database(settings.database_url)
    await db.connect()
    await initialize_schema(db)

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

    brain_a = CerebroA(settings.openai_chat_model, openai_client)
    brain_b = CerebroB(settings.openai_chat_model, openai_client)
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
    youtube_client = YouTubeClient(settings)
    live_client = LiveChatClient(youtube_client)

    # Limpeza unica de dados antigos na inicializacao
    await cleanup_on_startup(db, settings.memory_retention_days)

    # ── TTS WebSocket server (embedded, reachable publicly) ──────
    tts_ws = TtsWebSocketServer(
        db=db,
        host=settings.tts_ws_host,
        port=settings.tts_ws_port,
        poll_interval=2.0,
        settings=settings,
    )
    await tts_ws.start()

    # ── Resolver channel ID a partir do @handle ──────────────────────
    live_video_id: str | None = None
    if settings.youtube_live_url:
        live_video_id = extract_youtube_video_id(settings.youtube_live_url)
        if not live_video_id:
            raise RuntimeError(
                "YOUTUBE_LIVE_URL deve ser uma URL de live do YouTube ou um video_id valido."
            )
        logger.info("Modo live direta ativo: video_id=%s", live_video_id)

    channel_id: str | None = None
    if settings.youtube_channel_handle and not live_video_id:
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

    if not live_video_id and not channel_id and not settings.youtube_video_ids:
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
        return

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
                        connect_message=settings.youtube_live_connect_message,
                        known_live_ids=known_live_ids,
                    )
                elif channel_id and live_discovery_enabled:
                    live_discovery_enabled = await discover_and_connect_lives(
                        youtube_client=youtube_client,
                        live_client=live_client,
                        director=director,
                        channel_id=channel_id,
                        bot_channel_id=settings.youtube_bot_channel_id,
                        connect_message=settings.youtube_live_connect_message,
                        known_live_ids=known_live_ids,
                    )
            except YouTubeQuotaExceededError:
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
                        settings.youtube_bot_channel_id, last_seen_by_video,
                    )
                except YouTubeQuotaExceededError:
                    quota_pause_until = now + 3600
                    logger.warning("Quota do YouTube esgotada nos comentarios. Pausando por 1 hora.")

            await asyncio.sleep(settings.poll_interval_seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bot encerrado pelo usuario (Ctrl+C).")
    finally:
        await tts_ws.stop()
        await db.close()


# ── Live Discovery ────────────────────────────────────────────────────────

async def connect_to_live_video(
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    video_id: str,
    bot_channel_id: str,
    connect_message: str,
    known_live_ids: set[str],
) -> None:
    """Conecta diretamente a uma live conhecida por URL/video_id."""
    try:
        live_chat_id = await youtube_client.get_active_live_chat_id(video_id)
    except YouTubeQuotaExceededError:
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
            connect_message=connect_message,
        )
    )


async def discover_and_connect_lives(
    youtube_client: YouTubeClient,
    live_client: LiveChatClient,
    director: Director,
    channel_id: str,
    bot_channel_id: str,
    connect_message: str,
    known_live_ids: set[str],
) -> bool:
    """Busca lives ativas no canal e conecta nas que ainda nao foram vistas."""
    try:
        lives = await youtube_client.get_active_lives(channel_id)
    except YouTubeQuotaExceededError:
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
    connect_message: str,
) -> None:
    """Loop de polling do chat ao vivo de uma live especifica."""
    logger.info("Conectado ao chat ao vivo: %s (video=%s)", live_chat_id, video_id)

    # Envia mensagem de "bot ativado" no chat (ignora DRY_RUN)
    if connect_message:
        try:
            await live_client.post_message(live_chat_id, connect_message, force=True)
            logger.info("Mensagem de conexao enviada no chat %s.", live_chat_id)
        except Exception:
            logger.exception("Falha ao enviar mensagem de conexao no chat %s.", live_chat_id)

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
                    if bot_channel_id and msg.author_channel_id == bot_channel_id:
                        continue
                    await process_live_message(live_client, director, msg, live_chat_id)

            # YouTube recomenda esperar o pollingIntervalMillis
            await asyncio.sleep(poll_interval_ms / 1000.0)

        except YouTubeQuotaExceededError:
            logger.warning(
                "Cota da API do YouTube esgotada no chat %s. Pausando live chat.",
                live_chat_id,
            )
            return  # Stop immediately — no point retrying until quota resets
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
                return
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
                return
            await asyncio.sleep(10)


async def process_live_message(
    live_client: LiveChatClient,
    director: Director,
    message: YouTubeLiveMessage,
    live_chat_id: str,
) -> None:
    """Processa uma mensagem do chat ao vivo e responde."""
    try:
        reply = await director.decide_and_respond(
            user_message=message.text,
            user_youtube_id=message.author_channel_id or message.author_name,
            display_name=message.author_name,
            message_type="live",
        )
        thought, message_text = parse_thinking_response(reply.text)
        if thought:
            logger.info("Pensamento do bot: %s", thought)

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
    last_seen_by_video: dict,
) -> None:
    for video_id, last_seen in list(last_seen_by_video.items()):
        try:
            comments = await youtube_client.get_new_comments(video_id, last_seen)
        except YouTubeQuotaExceededError:
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
            if not (bot_channel_id and c.author_channel_id == bot_channel_id)
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
        reply = await director.decide_and_respond(
            user_message=comment.text,
            user_youtube_id=comment.author_channel_id or comment.author_name,
            display_name=comment.author_name,
            message_type="comment",
        )
        thought, message_text = parse_thinking_response(reply.text)
        if thought:
            logger.info("Pensamento do bot: %s", thought)

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
