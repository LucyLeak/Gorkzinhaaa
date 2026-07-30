from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from youtube_bot.utils.helpers import MAX_CHAT_MESSAGE_CHARS, prepare_chat_message


class ChatClient(Protocol):
    chat: object


@dataclass
class Brain:
    name: str
    prompt_base: str
    default_temperature: float
    model: str
    client: ChatClient | None = None

    async def generate(
        self,
        context: list[str],
        user_message: str,
        temperature: float | None = None,
        extra_instructions: str | None = None,
    ) -> str:
        if self.client is None:
            return self._dry_answer(user_message, extra_instructions)

        messages = [
            {"role": "system", "content": self.prompt_base},
            {
                "role": "system",
                "content": (
                    "A resposta publica enviada ao chat deve ter no maximo "
                    f"{MAX_CHAT_MESSAGE_CHARS} caracteres. Se usar <think>...</think>, "
                    "esse bloco sera removido antes de enviar; deixe a resposta final fora dele."
                ),
            },
            {
                "role": "system",
                "content": "Contexto recuperado da memoria: "
                + ("\n".join(context) if context else "sem memoria relevante."),
            },
        ]
        if extra_instructions:
            messages.append({"role": "system", "content": extra_instructions})
        messages.append({"role": "user", "content": user_message})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.default_temperature,
            max_tokens=220,
        )
        content = response.choices[0].message.content or ""
        return content.strip()

    async def generate_with_feedback(
        self,
        original_message: str,
        context: list[str],
        feedback: str,
    ) -> str:
        instructions = (
            "Sua resposta foi rejeitada por: "
            f"{feedback}. Reescreva corrigindo o problema, sem mencionar a validacao."
        )
        return await self.generate(
            context=context,
            user_message=original_message,
            extra_instructions=instructions,
        )

    def _dry_answer(self, user_message: str, feedback: str | None = None) -> str:
        suffix = f" Ajuste solicitado: {feedback}" if feedback else ""
        if self.name == "cerebro_b":
            answer = f"Modo teste: humor leve para: {user_message}{suffix}"
        else:
            answer = f"Modo teste: resposta objetiva para: {user_message}{suffix}"
        return prepare_chat_message(answer)[1]
