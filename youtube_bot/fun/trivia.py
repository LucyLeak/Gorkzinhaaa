from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TriviaQuestion:
    question: str
    answer: str
    points: int = 1


class TriviaGame:
    def __init__(self, questions_path: Path) -> None:
        self.questions_path = questions_path
        self.questions = self._load_questions()
        self.active_by_user: dict[int, TriviaQuestion] = {}

    def start(self, user_id: int) -> str:
        if not self.questions:
            return "Quiz sem perguntas configuradas ainda."
        question = random.choice(self.questions)
        self.active_by_user[user_id] = question
        return f"Quiz relampago: {question.question}"

    def answer(self, user_id: int, text: str) -> tuple[bool, int, str]:
        question = self.active_by_user.get(user_id)
        if question is None:
            return False, 0, "Nao tem quiz ativo para voce agora."

        normalized = text.strip().lower()
        expected = question.answer.strip().lower()
        if expected in normalized:
            self.active_by_user.pop(user_id, None)
            return True, question.points, f"Acertou. +{question.points} ponto(s)."
        return False, 0, "Ainda nao. Tenta de novo ou manda !quiz para outra pergunta."

    def _load_questions(self) -> list[TriviaQuestion]:
        if not self.questions_path.exists():
            return []
        with self.questions_path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        return [
            TriviaQuestion(
                question=str(item["question"]),
                answer=str(item["answer"]),
                points=int(item.get("points", 1)),
            )
            for item in raw
        ]
