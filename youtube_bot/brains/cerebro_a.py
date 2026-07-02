from __future__ import annotations

from youtube_bot.brains.base import Brain, ChatClient


class CerebroA(Brain):
    def __init__(self, model: str, client: ChatClient | None = None) -> None:
        super().__init__(
            name="cerebro_a",
            prompt_base=(
                "Voce e um assistente serio, objetivo e bem informado. "
                "Responda com clareza e profundidade."
            ),
            default_temperature=0.3,
            model=model,
            client=client,
        )
