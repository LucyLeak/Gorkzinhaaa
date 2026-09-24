from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from youtube_bot.utils.helpers import MAX_CHAT_MESSAGE_CHARS, prepare_chat_message

logger = logging.getLogger(__name__)

JSON_RESPONSE_CONTRACT = (
    "Responda SOMENTE com um objeto JSON valido, sem markdown ou texto fora do JSON, "
    'no formato {"thought":"<raciocinio interno, nunca enviado ao chat>",'
    '"message":"<mensagem final enviada ao chat>"}. '
    "O campo message deve conter apenas a resposta publica e ter no maximo "
    f"{MAX_CHAT_MESSAGE_CHARS} caracteres."
)


class ChatClient(Protocol):
    chat: object


@dataclass
class Brain:
    name: str
    prompt_base: str
    default_temperature: float
    model: str
    client: ChatClient | None = None
    json_mode: bool = True

    async def generate(
        self,
        context: list[str],
        user_message: str,
        temperature: float | None = None,
        extra_instructions: str | None = None,
        personality_instruction: str | None = None,
    ) -> str:
        """Gera a resposta e devolve SOMENTE a mensagem publica (texto limpo).

        O parse (JSON/<think>/plain-text com anti-leak) acontece aqui, uma unica
        vez por candidato: o thought e logado e nunca sai daqui; o validator e o
        main recebem a message ja extraida e nao re-parseiam.
        """
        content = await self._raw_completion(
            context=context,
            user_message=user_message,
            temperature=temperature,
            extra_instructions=extra_instructions,
            personality_instruction=personality_instruction,
        )
        thought, message = prepare_chat_message(content)
        if thought:
            logger.info("Pensamento do bot (%s): %s", self.name, thought)
        return message

    async def _raw_completion(
        self,
        context: list[str],
        user_message: str,
        temperature: float | None,
        extra_instructions: str | None,
        personality_instruction: str | None,
    ) -> str:
        if self.client is None:
            return self._dry_answer(user_message, extra_instructions, personality_instruction)

        system_prompt = self.prompt_base
        if personality_instruction:
            system_prompt = f"{personality_instruction}\n\n{self.prompt_base}"
        logger.debug("System prompt final (%s): %r", self.name, system_prompt)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "system",
                "content": JSON_RESPONSE_CONTRACT,
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
            **(
                {"response_format": {"type": "json_object"}}
                if getattr(self, "json_mode", True)
                else {}
            ),
        )
        return (response.choices[0].message.content or "").strip()

    async def generate_with_feedback(
        self,
        original_message: str,
        context: list[str],
        feedback: str,
        personality_instruction: str | None = None,
        temperature: float | None = None,
    ) -> str:
        instructions = (
            "Sua resposta foi rejeitada por: "
            f"{feedback}. Reescreva corrigindo o problema, sem mencionar a validacao."
        )
        return await self.generate(
            context=context,
            user_message=original_message,
            temperature=temperature,
            extra_instructions=instructions,
            personality_instruction=personality_instruction,
        )

    def _dry_answer(
        self,
        user_message: str,
        feedback: str | None = None,
        personality_instruction: str | None = None,
    ) -> str:
        suffix = f" Ajuste solicitado: {feedback}" if feedback else ""
        personality_tag = " [personalidade aplicada]" if personality_instruction else ""
        if self.name == "cerebro_b":
            answer = f"Modo teste: humor leve para: {user_message}{personality_tag}{suffix}"
        else:
            answer = f"Modo teste: resposta objetiva para: {user_message}{personality_tag}{suffix}"
        return json.dumps(
            {
                "thought": "Resposta simulada no modo DRY_RUN.",
                "message": answer,
            },
            ensure_ascii=False,
        )
