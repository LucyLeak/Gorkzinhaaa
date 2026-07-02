from __future__ import annotations

from dataclasses import dataclass

from youtube_bot.brains.base import Brain
from youtube_bot.memory.vector_store import VectorMemoryStore
from youtube_bot.validation.metrics import cosine_similarity


@dataclass
class ValidationResult:
    approved: bool
    answer: str | None
    brain_name: str
    reasons: list[str]
    attempts: int


class Validator:
    def __init__(
        self,
        forbidden_words: tuple[str, ...],
        coherence_threshold: float,
        vector_store: VectorMemoryStore | None = None,
    ) -> None:
        self.forbidden_words = tuple(word.lower() for word in forbidden_words)
        self.coherence_threshold = coherence_threshold
        self.vector_store = vector_store

    async def validate_and_repair(
        self,
        question: str,
        context: list[str],
        brain: Brain,
        max_attempts: int,
    ) -> ValidationResult:
        answer = await brain.generate(context=context, user_message=question)
        attempts = 1

        while attempts <= max_attempts:
            reasons = await self.validate(question, answer, brain.name)
            if not reasons:
                return ValidationResult(True, answer, brain.name, [], attempts)
            if attempts == max_attempts:
                return ValidationResult(False, answer, brain.name, reasons, attempts)
            answer = await brain.generate_with_feedback(
                original_message=question,
                context=context,
                feedback="; ".join(reasons),
            )
            attempts += 1

        return ValidationResult(False, answer, brain.name, ["tentativas esgotadas"], attempts)

    async def validate(self, question: str, answer: str, brain_name: str) -> list[str]:
        reasons: list[str] = []
        lower = answer.lower()

        if any(word and word in lower for word in self.forbidden_words):
            reasons.append("conteudo bloqueado por palavra proibida")
        if len(answer.strip()) < 10:
            reasons.append("resposta muito curta")
        if len(answer) > 500:
            reasons.append("resposta acima de 500 caracteres")

        coherence = await self._coherence(question, answer)
        if coherence is not None and coherence < self.coherence_threshold:
            reasons.append(f"baixa coerencia semantica ({coherence:.2f})")

        reasons.extend(self._personality_reasons(answer, brain_name))
        return reasons

    async def _coherence(self, question: str, answer: str) -> float | None:
        if self.vector_store is None or not self.vector_store.can_embed:
            return None
        question_embedding = await self.vector_store.embed_text(question)
        answer_embedding = await self.vector_store.embed_text(answer)
        return cosine_similarity(question_embedding, answer_embedding)

    def _personality_reasons(self, answer: str, brain_name: str) -> list[str]:
        lower = answer.lower()
        if brain_name == "cerebro_a" and any(token in lower for token in ("kkkk", "haha")):
            return ["tom humoristico demais para o cerebro A"]
        if brain_name == "cerebro_b" and len(answer.split()) > 80:
            return ["resposta longa demais para o cerebro B"]
        return []
