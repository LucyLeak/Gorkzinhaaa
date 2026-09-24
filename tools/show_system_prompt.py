"""Mostra o system prompt final e a temperatura que cada cerebro enviaria ao
provider para as personalidades 'amigo' e 'inimigo' (revisao item 3b).

Nao faz chamadas de rede: usa um client de captura local que registra o payload
exato que iria para POST /chat/completions. O log DEBUG "System prompt final"
vem de Brain._raw_completion (o mesmo caminho de producao).

Uso (a partir da raiz do repositorio):
    python tools/show_system_prompt.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from youtube_bot.brains.cerebro_a import CerebroA
from youtube_bot.brains.cerebro_b import CerebroB
from youtube_bot.brains.diretor import (
    PERSONALITY_TEMPERATURE_DELTAS,
    Director,
)

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

USER_MESSAGE = "fala galera, o que voces acharam do video de hoje?"


class _CaptureCompletions:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def create(self, **kwargs):
        self.payloads.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content='{"thought":"demo","message":"ok"}'
                    )
                )
            ]
        )


class _CaptureChat:
    def __init__(self) -> None:
        self.completions = _CaptureCompletions()


class _CaptureClient:
    def __init__(self) -> None:
        self.chat = _CaptureChat()


async def demo() -> None:
    cenarios = [
        ("usuario AMIGO -> cerebro_a", CerebroA, "amigo"),
        ("usuario INIMIGO -> cerebro_b", CerebroB, "inimigo"),
    ]
    for label, brain_cls, personality in cenarios:
        client = _CaptureClient()
        brain = brain_cls(model="command-r-plus-08-2024", client=client, json_mode=False)
        instruction = Director._personality_instruction(personality, None)
        temperature = Director._personality_temperature(brain, personality)
        print("=" * 72)
        print(f"Cenario: {label}")
        print(f"personality_instruction repassada ao cerebro:\n  {instruction!r}")
        await brain.generate(
            context=[],
            user_message=USER_MESSAGE,
            personality_instruction=instruction,
            temperature=temperature,
        )
        payload = client.chat.completions.payloads[0]
        print(
            f"temperature enviada: {payload['temperature']} "
            f"(delta={PERSONALITY_TEMPERATURE_DELTAS.get(personality, 0.0)})"
        )
        print("messages enviadas ao provider:")
        for m in payload["messages"]:
            print(f"  [{m['role']}] {m['content']!r}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(demo())
