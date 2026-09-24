"""Testes do diretor: personalidade aplicada aos cerebros, usuario 'bloqueado'
pulado por completo e 'neutro' com notas usando apenas as notas (decisao B).

Cobertura:
- bloqueado  -> reason='bloqueado', texto vazio, NENHUMA chamada de IA,
                mensagem nao registrada, memoria nao consultada/gravada.
- amigo      -> system prompt do cerebro comeca com a instrucao de amigo;
                temperatura = default + 0.1.
- inimigo    -> instrucao de rival sarcastico; temperatura = default + 0.2.
- evitar     -> instrucao de resposta minima; temperatura = default - 0.2.
- neutro/NULL-> system prompt identico ao prompt_base e temperatura default.
- neutro+notas -> instruction contem APENAS as notas (sem instrucao de tom).

Execucao (a partir da raiz do repositorio):
    python -m unittest tests.test_diretor_personality -v
Somente biblioteca padrao (unittest + mock); nenhuma dependencia nova.
"""

from __future__ import annotations

import sys
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from youtube_bot.brains.base import Brain
from youtube_bot.brains.diretor import Director
from youtube_bot.validation.validator import Validator

# Resposta que o client fake devolve (JSON contratado); Brain.generate extrai
# a mensagem publica dele. Tem >= 10 chars para passar no validator.
BRAIN_REPLY = '{"thought": "pensando", "message": "resposta suficiente pro chat"}'
# Mensagem neutra: sem keywords de humor/pergunta e sentimento 0 -> cerebro_a
# sempre escolhido (brain_surprise_chance=0.0 elimina a troca aleatoria).
USER_MESSAGE = "oi tudo bem por ai"


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=BRAIN_REPLY)
                )
            ]
        )


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


def _make_brain(name: str, temperature: float) -> tuple[Brain, _FakeCompletions]:
    client = _FakeClient()
    brain = Brain(
        name=name,
        prompt_base=f"PROMPT {name.upper()}",
        default_temperature=temperature,
        model="modelo-teste",
        client=client,
        json_mode=False,
    )
    return brain, client.chat.completions


def _make_director(user_row: dict):
    brain_a, completions_a = _make_brain("cerebro_a", 0.3)
    brain_b, completions_b = _make_brain("cerebro_b", 0.9)
    validator = Validator(forbidden_words=(), coherence_threshold=0.60, vector_store=None)
    vector_store = unittest.mock.Mock()
    vector_store.retrieve_similar_memories = unittest.mock.AsyncMock(return_value=[])
    vector_store.store_memory = unittest.mock.AsyncMock(return_value=None)
    settings = types.SimpleNamespace(max_repair_attempts=1, brain_surprise_chance=0.0)
    director = Director(
        brain_a=brain_a,
        brain_b=brain_b,
        db=unittest.mock.Mock(),
        validator=validator,
        vector_store=vector_store,
        settings=settings,
        trivia=None,
        giphy=None,
        consolidator=None,
    )
    return director, completions_a, completions_b, vector_store


def _patch_models(user_row: dict):
    """Patching dos modelos usados pelo diretor; devolve (ctx, insert_message_mock)."""
    insert_message = unittest.mock.AsyncMock(return_value=1)
    ctx = unittest.mock.patch.multiple(
        "youtube_bot.db.models",
        upsert_user=unittest.mock.AsyncMock(return_value=user_row),
        insert_message=insert_message,
        insert_generated_response=unittest.mock.AsyncMock(return_value=1),
        update_brain_outcome=unittest.mock.AsyncMock(return_value=None),
    )
    return ctx, insert_message


def _system_prompt(completions: _FakeCompletions) -> str:
    messages = completions.calls[0]["messages"]
    return next(m["content"] for m in messages if m["role"] == "system")


class TestDirectorPersonality(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_user_skips_reply(self):
        row = {"id": 1, "personalidade": "bloqueado", "personalidade_notas": ""}
        director, completions_a, completions_b, vector_store = _make_director(row)
        models_ctx, insert_message = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC111", "Bloqueado")
        self.assertEqual(reply.reason, "bloqueado")
        self.assertEqual(reply.text, "")
        self.assertFalse(completions_a.calls, "cerebro_a nao deve ser chamado")
        self.assertFalse(completions_b.calls, "cerebro_b nao deve ser chamado")
        insert_message.assert_not_called()
        vector_store.retrieve_similar_memories.assert_not_called()
        vector_store.store_memory.assert_not_called()

    async def test_friend_user_applies_instruction_and_temperature(self):
        row = {"id": 2, "personalidade": "amigo", "personalidade_notas": ""}
        director, completions_a, _, _ = _make_director(row)
        models_ctx, _ = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC222", "Amigo")
        self.assertEqual(reply.reason, "normal")
        self.assertTrue(completions_a.calls)
        system = _system_prompt(completions_a)
        self.assertTrue(system.startswith("Você trata este usuário como um amigo próximo"))
        self.assertIn("PROMPT CEREBRO_A", system)
        self.assertAlmostEqual(completions_a.calls[0]["temperature"], 0.4)  # 0.3 + 0.1

    async def test_enemy_user_applies_instruction_and_temperature(self):
        row = {"id": 3, "personalidade": "inimigo", "personalidade_notas": ""}
        director, completions_a, _, _ = _make_director(row)
        models_ctx, _ = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC333", "Inimigo")
        self.assertEqual(reply.reason, "normal")
        system = _system_prompt(completions_a)
        self.assertTrue(system.startswith("Você não gosta deste usuário"))
        self.assertIn("PROMPT CEREBRO_A", system)
        self.assertAlmostEqual(completions_a.calls[0]["temperature"], 0.5)  # 0.3 + 0.2

    async def test_avoid_user_applies_instruction_and_temperature(self):
        row = {"id": 4, "personalidade": "evitar", "personalidade_notas": ""}
        director, completions_a, _, _ = _make_director(row)
        models_ctx, _ = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC444", "Evitar")
        self.assertEqual(reply.reason, "normal")
        system = _system_prompt(completions_a)
        self.assertTrue(system.startswith("Você quer encerrar a conversa"))
        self.assertAlmostEqual(completions_a.calls[0]["temperature"], 0.1)  # 0.3 - 0.2

    async def test_neutral_user_stays_identical(self):
        row = {"id": 5, "personalidade": None, "personalidade_notas": None}
        director, completions_a, _, _ = _make_director(row)
        models_ctx, _ = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC555", "Neutro")
        self.assertEqual(reply.reason, "normal")
        self.assertEqual(_system_prompt(completions_a), "PROMPT CEREBRO_A")
        self.assertAlmostEqual(completions_a.calls[0]["temperature"], 0.3)  # default

    async def test_neutral_with_notes_uses_notes_only(self):
        # Decisao (B): notas sao override manual e valem mesmo com 'neutro',
        # mas nenhuma instrucao de tom e adicionada.
        row = {
            "id": 6,
            "personalidade": "neutro",
            "personalidade_notas": "Conheceu o canal na live de sexta",
        }
        director, completions_a, _, _ = _make_director(row)
        models_ctx, _ = _patch_models(row)
        with models_ctx:
            reply = await director.decide_and_respond(USER_MESSAGE, "UC666", "NeutroNotas")
        self.assertEqual(reply.reason, "normal")
        system = _system_prompt(completions_a)
        self.assertEqual(
            system,
            "Notas sobre este usuario: Conheceu o canal na live de sexta\n\nPROMPT CEREBRO_A",
        )
        self.assertNotIn("trata este usuário", system)
        self.assertAlmostEqual(completions_a.calls[0]["temperature"], 0.3)  # default


if __name__ == "__main__":
    unittest.main()
