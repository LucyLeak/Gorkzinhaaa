"""Testes unitarios do parser de respostas da IA e da personalidade dos cerebros.

Cobertura (Task 1c + 2c):
- JSON valido                      -> parsed corretamente (thought, message).
- Tags <think>...</think>          -> parsed corretamente.
- Texto puro                       -> publicado como mensagem, com aviso
                                      "Plain-text fallback used for brain reply."
- JSON malformado iniciando com "{" e contendo "thought" -> bloqueado ("", "").
- personality_instruction          -> prepended ao system prompt dos cerebros;
                                      None -> comportamento identico ao anterior.

Execucao (a partir da raiz do repositorio):
    python -m unittest tests.test_prepare_chat_message -v
ou diretamente:
    python tests/test_prepare_chat_message.py
Somente biblioteca padrao (unittest); nenhuma dependencia nova.
"""

from __future__ import annotations

import json
import logging
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from youtube_bot.brains.base import Brain
from youtube_bot.utils.helpers import MAX_CHAT_MESSAGE_CHARS, prepare_chat_message


class TestPrepareChatMessage(unittest.TestCase):
    def test_valid_json_is_parsed(self):
        thought, message = prepare_chat_message(
            '{"thought": "raciocinio interno", "message": "Ola, tudo bem?"}'
        )
        self.assertEqual(thought, "raciocinio interno")
        self.assertEqual(message, "Ola, tudo bem?")

    def test_valid_json_inside_code_fence_is_parsed(self):
        thought, message = prepare_chat_message(
            '```json\n{"thought": "t", "message": "resposta"}\n```'
        )
        self.assertEqual(thought, "t")
        self.assertEqual(message, "resposta")

    def test_think_tags_are_parsed(self):
        thought, message = prepare_chat_message(
            "<think>pensando muito aqui</think>Resposta final para o chat."
        )
        self.assertEqual(thought, "pensando muito aqui")
        self.assertEqual(message, "Resposta final para o chat.")

    def test_plain_text_falls_back_to_message_with_warning(self):
        # Caminho padrao do parse unico: Brain.generate chama com defaults.
        with self.assertLogs("youtube_bot.utils.helpers", level="WARNING") as captured:
            thought, message = prepare_chat_message(
                "Desculpe, nao entendi bem. Pode reformular?"
            )
        self.assertTrue(
            any("Plain-text fallback used for brain reply." in line for line in captured.output)
        )
        self.assertEqual(thought, "")
        self.assertEqual(message, "Desculpe, nao entendi bem. Pode reformular?")

    def test_allow_false_blocks_plain_text_by_policy(self):
        # Fail-closed para quem pede saida estruturada explicitamente.
        with self.assertLogs("youtube_bot.utils.helpers", level="WARNING") as captured:
            thought, message = prepare_chat_message(
                "Desculpe, nao entendi bem. Pode reformular?",
                allow_plain_text=False,
            )
        self.assertTrue(
            any("Plain-text fallback blocked by policy." in line for line in captured.output)
        )
        self.assertEqual((thought, message), ("", ""))

    def test_leak_like_plain_text_is_blocked(self):
        # "Pensando em voz alta" e bloqueado antes de chegar ao chat.
        with self.assertLogs("youtube_bot.utils.helpers", level="WARNING") as captured:
            thought, message = prepare_chat_message(
                "Vou responder de forma engraçada: oloko é o cara mais..."
            )
        self.assertTrue(
            any("looks like reasoning" in line for line in captured.output)
        )
        self.assertEqual((thought, message), ("", ""))

    def test_user_reasoning_question_is_blocked(self):
        thought, message = prepare_chat_message(
            "O usuário está perguntando sobre X. Devo responder Y."
        )
        self.assertEqual((thought, message), ("", ""))

    def test_leak_check_can_be_disabled(self):
        # Escape hatch: anti_leak=False publica o texto mesmo parecendo razocinio.
        _, message = prepare_chat_message(
            "Vou te contar uma piada", anti_leak=False
        )
        self.assertEqual(message, "Vou te contar uma piada")

    def test_malformed_json_with_thought_is_blocked(self):
        thought, message = prepare_chat_message(
            '{"thought": "raciocinio vazando", "message": "oi"',
            allow_plain_text=False,
        )
        self.assertEqual((thought, message), ("", ""))

    def test_malformed_json_without_thought_is_blocked_for_brains(self):
        thought, message = prepare_chat_message(
            '{"message": "oi', allow_plain_text=False
        )
        self.assertEqual((thought, message), ("", ""))

    def test_think_only_reply_yields_empty_message(self):
        # O thought volta para log, mas a mensagem fica vazia e a postagem
        # e bloqueada mais a frente (reason=empty_after_parse).
        thought, message = prepare_chat_message(
            "<think>somente raciocinio, nada publicavel</think>",
            allow_plain_text=False,
        )
        self.assertEqual(thought, "somente raciocinio, nada publicavel")
        self.assertEqual(message, "")

    def test_empty_input_returns_empty(self):
        self.assertEqual(prepare_chat_message(""), ("", ""))

    def test_long_message_is_limited(self):
        text = '{"thought": "", "message": "' + "a" * 300 + '"}'
        _, message = prepare_chat_message(text)
        self.assertLessEqual(len(message), MAX_CHAT_MESSAGE_CHARS)

    @staticmethod
    def _attach_null_handler():
        handler = logging.NullHandler()
        logging.getLogger("youtube_bot.utils.helpers").addHandler(handler)
        return handler


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=self._content)
                )
            ]
        )


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)


def _make_brain(content: str) -> tuple[Brain, _FakeCompletions]:
    client = _FakeClient(content)
    brain = Brain(
        name="cerebro_a",
        prompt_base="PROMPT BASE",
        default_temperature=0.3,
        model="modelo-teste",
        client=client,
        json_mode=False,
    )
    return brain, client.chat.completions


class TestBrainPersonalityInstruction(unittest.IsolatedAsyncioTestCase):
    async def test_personality_instruction_is_prepended_to_system_prompt(self):
        brain, completions = _make_brain('{"thought":"t","message":"ok"}')
        await brain.generate(
            context=[], user_message="oi", personality_instruction="PERSONA DE TESTE"
        )
        system_contents = [
            m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system"
        ]
        self.assertTrue(system_contents[0].startswith("PERSONA DE TESTE"))
        self.assertIn("PROMPT BASE", system_contents[0])

    async def test_feedback_regeneration_keeps_personality(self):
        brain, completions = _make_brain('{"thought":"t","message":"ok"}')
        await brain.generate_with_feedback(
            original_message="oi",
            context=[],
            feedback="resposta muito curta",
            personality_instruction="PERSONA DE TESTE",
        )
        system_contents = [
            m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system"
        ]
        self.assertTrue(system_contents[0].startswith("PERSONA DE TESTE"))

    async def test_no_personality_keeps_prompt_identical(self):
        brain, completions = _make_brain('{"thought":"t","message":"ok"}')
        await brain.generate(context=[], user_message="oi")
        messages = completions.calls[0]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "PROMPT BASE"})

    async def test_dry_answer_marks_personality_and_returns_clean_text(self):
        brain = Brain(
            name="cerebro_a",
            prompt_base="PROMPT BASE",
            default_temperature=0.3,
            model="modelo-teste",
            client=None,
            json_mode=False,
        )
        # No parse unico, generate devolve a mensagem publica (texto limpo).
        message = await brain.generate(
            context=[], user_message="oi", personality_instruction="PERSONA"
        )
        self.assertIn("[personalidade aplicada]", message)
        self.assertFalse(message.lstrip().startswith("{"))

    async def test_dry_answer_without_personality_is_unchanged(self):
        brain = Brain(
            name="cerebro_a",
            prompt_base="PROMPT BASE",
            default_temperature=0.3,
            model="modelo-teste",
            client=None,
            json_mode=False,
        )
        message = await brain.generate(context=[], user_message="oi")
        self.assertNotIn("[personalidade aplicada]", message)


if __name__ == "__main__":
    unittest.main()
