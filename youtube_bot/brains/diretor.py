from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from youtube_bot.brains.base import Brain
from youtube_bot.config import Settings
from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.fun.challenges import emoji_reaction, mirror_mode, quick_rhyme
from youtube_bot.fun.giphy import GiphyClient
from youtube_bot.fun.trivia import TriviaGame
from youtube_bot.fun.tts import handle_tts_command
from youtube_bot.memory.consolidator import MemoryConsolidator
from youtube_bot.memory.vector_store import VectorMemoryStore
from youtube_bot.utils.helpers import (
    HUMOR_KEYWORDS,
    QUESTION_KEYWORDS,
    has_any_keyword,
    normalize_text,
)
from youtube_bot.validation.metrics import simple_sentiment_score
from youtube_bot.validation.validator import ValidationResult, Validator

logger = logging.getLogger(__name__)

# Instrucoes de personalidade injetadas no inicio do system prompt de AMBOS os
# cerebros. 'neutro' nao gera instrucao de tom; 'bloqueado' nem chega a gerar
# resposta.
PERSONALITY_INSTRUCTIONS: dict[str, str] = {
    "amigo": (
        "Você trata este usuário como um amigo próximo. É gentil, caloroso e "
        "demonstra que gosta dele: faça elogios sinceros quando couber, concorde "
        "com ele com frequência e puxe assunto como quem quer continuar a "
        "conversa. Tom informal e afetuoso."
    ),
    "inimigo": (
        "Você não gosta deste usuário. É sarcástico, irônico e responde com "
        "deboche: provoque e implique com ele sempre que possível, mas SEM "
        "ofender de verdade — nada de xingamento pesado, racismo, homofobia ou "
        "qualquer violação das diretrizes do YouTube. O tom é de 'amigo que "
        "vive implicando', não de inimigo de verdade."
    ),
    "evitar": (
        "Você quer encerrar a conversa rápido com este usuário. Responda com o "
        "mínimo necessário, monossílabos quando possível, sem fazer perguntas, "
        "sem puxar assunto e sem contar piadas. Se ele insistir, responda de "
        "forma seca e educada."
    ),
}

# Ajuste de temperatura por personalidade, aplicado sobre a temperatura padrao
# do cerebro escolhido e limitado a [0.0, 1.0] (ver _personality_temperature).
# 'neutro'/'bloqueado' nao alteram a temperatura.
PERSONALITY_TEMPERATURE_DELTAS: dict[str, float] = {
    "amigo": 0.1,
    "inimigo": 0.2,
    "evitar": -0.2,
}


@dataclass
class DirectorReply:
    text: str
    brain_name: str
    approved: bool
    reasons: list[str]
    # Motivo do despacho: "normal" (resposta gerada) ou "bloqueado" (usuario
    # com personalidade 'bloqueado'; nenhuma resposta gerada). O motivo
    # "empty_after_parse" e registrado em process_live_message/process_comment
    # quando a mensagem fica vazia apos o parse.
    reason: str = "normal"


class Director:
    def __init__(
        self,
        brain_a: Brain,
        brain_b: Brain,
        db: Database,
        validator: Validator,
        vector_store: VectorMemoryStore,
        settings: Settings,
        trivia: TriviaGame | None = None,
        giphy: GiphyClient | None = None,
        consolidator: MemoryConsolidator | None = None,
    ) -> None:
        self.brain_a = brain_a
        self.brain_b = brain_b
        self.db = db
        self.validator = validator
        self.vector_store = vector_store
        self.settings = settings
        self.trivia = trivia
        self.giphy = giphy
        self.consolidator = consolidator

    async def decide_and_respond(
        self,
        user_message: str,
        user_youtube_id: str,
        display_name: str | None,
        message_type: str = "comment",
        author_channel_id: str | None = None,
    ) -> DirectorReply:
        user = await models.upsert_user(
            self.db, user_youtube_id, display_name, author_channel_id
        )
        personality = user.get("personalidade") or "neutro"
        if personality == "bloqueado":
            # Usuario ignorado por completo: sem resposta, sem chamada de IA,
            # sem comandos fun e sem registro de memoria (economiza tokens/quota).
            return DirectorReply("", "diretor", False, [], reason="bloqueado")
        user_id = int(user["id"])
        await models.insert_message(self.db, user_id, user_message, message_type)

        personality_instruction = self._personality_instruction(
            personality, user.get("personalidade_notas")
        )
        if personality_instruction:
            logger.info(
                "Personality applied: %s for user %s.", personality, user_youtube_id
            )

        fun_reply = await self._maybe_handle_fun(user_id, user_message)
        if fun_reply:
            await models.insert_generated_response(
                self.db,
                user_id,
                user_message,
                fun_reply,
                "fun",
                True,
                None,
            )
            await self._remember(user_id, user_message, fun_reply)
            return DirectorReply(fun_reply, "fun", True, [])

        memories = await self.vector_store.retrieve_similar_memories(
            user_id=user_id,
            query_text=user_message,
            top_k=5,
        )
        selected_brain = self._choose_brain(user_message, user)
        result = await self.validator.validate_and_repair(
            question=user_message,
            context=memories,
            brain=selected_brain,
            max_attempts=self.settings.max_repair_attempts,
            personality_instruction=personality_instruction,
            temperature=self._personality_temperature(selected_brain, personality),
        )

        if not result.approved:
            await models.update_brain_outcome(self.db, user_id, selected_brain.name, False)
            alternate = self._alternate_brain(selected_brain.name)
            logger.info(
                "Alternando de %s para %s por falha de validacao: %s",
                selected_brain.name,
                alternate.name,
                "; ".join(result.reasons),
            )
            result = await self.validator.validate_and_repair(
                question=user_message,
                context=memories,
                brain=alternate,
                max_attempts=1,
                personality_instruction=personality_instruction,
                temperature=self._personality_temperature(alternate, personality),
            )

        reply = self._reply_from_result(result)
        await models.insert_generated_response(
            self.db,
            user_id,
            user_message,
            reply.text,
            reply.brain_name,
            reply.approved,
            "; ".join(reply.reasons) if reply.reasons else None,
        )

        if reply.brain_name in {"cerebro_a", "cerebro_b"}:
            await models.update_brain_outcome(
                self.db, user_id, reply.brain_name, reply.approved
            )

        await self._remember(user_id, user_message, reply.text)
        return reply

    def _choose_brain(self, message: str, user_history: dict) -> Brain:
        normalized = normalize_text(message)
        sentiment = simple_sentiment_score(normalized)

        preferred = self.brain_a
        if has_any_keyword(normalized, HUMOR_KEYWORDS) or sentiment > 0.25:
            preferred = self.brain_b
        if has_any_keyword(normalized, QUESTION_KEYWORDS) or sentiment < -0.20:
            preferred = self.brain_a

        if int(user_history.get("falhas_cerebro_a", 0)) >= 3:
            preferred = self.brain_b
        if int(user_history.get("falhas_cerebro_b", 0)) >= 3:
            preferred = self.brain_a

        a_success = int(user_history.get("sucesso_cerebro_a", 0))
        b_success = int(user_history.get("sucesso_cerebro_b", 0))
        if a_success + b_success >= 5:
            preferred = self.brain_b if b_success > a_success else self.brain_a

        if random.random() < self.settings.brain_surprise_chance:
            return self._alternate_brain(preferred.name)
        return preferred

    def _alternate_brain(self, brain_name: str) -> Brain:
        return self.brain_b if brain_name == self.brain_a.name else self.brain_a

    @staticmethod
    def _personality_instruction(personality: str, notes: str | None) -> str | None:
        parts: list[str] = []
        instruction = PERSONALITY_INSTRUCTIONS.get(personality)
        if instruction:
            parts.append(instruction)
        notes_text = (notes or "").strip()
        if notes_text:
            parts.append(f"Notas sobre este usuario: {notes_text}")
        if not parts:
            return None
        return " ".join(parts)

    @staticmethod
    def _personality_temperature(brain: Brain, personality: str) -> float | None:
        # Temperatura absoluta enviada ao provider: default do cerebro + delta
        # da personalidade, limitada a [0.0, 1.0] (o Cohere command-r rejeita
        # temperature > 1.0; sem o clamp, cerebro_b 0.9 + 0.2 = 1.1 daria 400).
        delta = PERSONALITY_TEMPERATURE_DELTAS.get(personality)
        if not delta:
            return None
        return min(max(brain.default_temperature + delta, 0.0), 1.0)

    def _reply_from_result(self, result: ValidationResult) -> DirectorReply:
        if result.approved and result.answer:
            return DirectorReply(result.answer, result.brain_name, True, [])
        return DirectorReply(
            "Desculpe, nao entendi bem. Pode reformular?",
            result.brain_name,
            False,
            result.reasons or ["validacao falhou"],
        )

    async def _maybe_handle_fun(self, user_id: int, message: str) -> str | None:
        normalized = normalize_text(message)

        reaction = emoji_reaction(message)
        if reaction:
            return reaction

        if self.trivia and ("!quiz" in normalized or " quiz" in f" {normalized}"):
            return self.trivia.start(user_id)

        if self.trivia and ("!resposta" in normalized or normalized.startswith("resposta")):
            is_correct, points, reply = self.trivia.answer(user_id, message)
            if is_correct and points:
                total = await models.add_points(self.db, user_id, points)
                return f"{reply} Total agora: {total}."
            return reply

        if "espelho" in normalized:
            return mirror_mode(message.replace("espelho", "", 1).strip() or message)

        if "rima" in normalized:
            word = normalized.split()[-1]
            return quick_rhyme(word)

        if self.giphy and self.giphy.enabled and "gif" in normalized:
            tag = "funny" if simple_sentiment_score(normalized) >= 0 else "reaction"
            url = await self.giphy.random_gif(tag)
            if url:
                return f"GIF encontrado: {url}"

        tts_reply = await handle_tts_command(message, user_id, self.settings, self.db)
        if tts_reply is not None:
            return tts_reply

        return None

    async def _remember(self, user_id: int, user_message: str, reply: str) -> None:
        await self.vector_store.store_memory(
            user_id=user_id,
            text=f"Usuario disse: {user_message}\nBot respondeu: {reply}",
            memory_type="episodio",
        )
        if self.consolidator:
            await self.consolidator.consolidate_if_needed(user_id)
