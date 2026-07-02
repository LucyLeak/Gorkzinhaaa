from __future__ import annotations

from youtube_bot.brains.base import Brain, ChatClient


class CerebroB(Brain):
    def __init__(self, model: str, client: ChatClient | None = None) -> None:
        super().__init__(
            name="cerebro_b",
            prompt_base=(
                "Voce e um comediante inteligente, cheio de ironia e piadas "
                "inteligentes. Seja divertido, mas nunca ofensivo."
            ),
            default_temperature=0.9,
            model=model,
            client=client,
        )
