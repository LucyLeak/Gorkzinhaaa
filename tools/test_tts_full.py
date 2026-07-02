"""
Teste completo do fluxo TTS — banco de dados + geração + Catbox + WebSocket.

Este script:
  1. Conecta no banco Neon
  2. Cria um usuário de teste (ou usa existente)
  3. Simula o comando !tts via handle_tts_command()
  4. Verifica o status no banco
  5. Conecta no WebSocket local e aguarda o broadcast
  6. Limpa os dados de teste

PRÉ-REQUISITOS:
  - .env configurado com NEON_DATABASE_URL
  - Bot NÃO precisa estar rodando (o script sobe o WS server internamente)

USO:
  python tools/test_tts_full.py
  python tools/test_tts_full.py "Frase personalizada"
"""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youtube_bot.config import load_settings
from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.fun.tts import handle_tts_command
from youtube_bot.tts_ws_server import TtsWebSocketServer
from youtube_bot.utils.logger import configure_logging

# ID de usuário de teste (fixo para não poluir usuários reais)
TEST_USER_YOUTUBE_ID = "__tts_test_user__"
TEST_USER_NAME = "TTS Tester"


async def main() -> None:
    configure_logging("INFO")
    settings = load_settings()

    if not settings.database_url:
        print("❌ NEON_DATABASE_URL não configurada no .env")
        return

    frase = sys.argv[1] if len(sys.argv) > 1 else "Teste completo do sistema TTS do bot Gorkzinhaaa!"

    db = Database(settings.database_url)

    try:
        # ── 1. Conectar no banco ────────────────────────────────────
        print("=" * 60)
        print("1. CONECTANDO NO BANCO DE DADOS")
        print("=" * 60)
        await db.connect()
        await models.initialize_schema(db)
        print("   ✅ Conectado ao Neon PostgreSQL")

        # ── 2. Criar usuário de teste ───────────────────────────────
        print()
        print("=" * 60)
        print("2. CRIANDO USUÁRIO DE TESTE")
        print("=" * 60)
        user = await models.upsert_user(db, TEST_USER_YOUTUBE_ID, TEST_USER_NAME)
        user_id = int(user["id"])
        print(f"   ✅ Usuário: id={user_id}, nome={user['nome']}")

        # ── 3. Subir servidor WebSocket local ───────────────────────
        print()
        print("=" * 60)
        print("3. INICIANDO SERVIDOR WEBSOCKET LOCAL")
        print("=" * 60)
        ws_port = 18765  # porta diferente para não conflitar com o bot
        ws_server = TtsWebSocketServer(
            db=db,
            host="127.0.0.1",
            port=ws_port,
            poll_interval=1.0,
            settings=settings,
        )
        await ws_server.start()
        print(f"   ✅ WebSocket server em ws://127.0.0.1:{ws_port}/ws")

        # ── 4. Conectar cliente WebSocket ───────────────────────────
        print()
        print("=" * 60)
        print("4. CONECTANDO CLIENTE WEBSOCKET")
        print("=" * 60)

        received_messages: list[dict] = []
        ws_connected = asyncio.Event()

        async def ws_listener() -> None:
            """Conecta no WS e coleta mensagens recebidas."""
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        f"ws://127.0.0.1:{ws_port}/ws",
                        heartbeat=5.0,
                    ) as ws:
                        print("   ✅ Cliente WebSocket conectado")
                        ws_connected.set()
                        # Keep listening — don't break on first message
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                received_messages.append(data)
                                print(f"   📩 Recebido: {json.dumps(data, indent=2)}")
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
            except Exception as exc:
                print(f"   ⚠️ Erro no cliente WS: {exc}")

        ws_task = asyncio.create_task(ws_listener())
        # Wait for the WS client to actually connect before proceeding
        await asyncio.wait_for(ws_connected.wait(), timeout=5.0)
        # Extra safety: give the poller one cycle to register the client
        await asyncio.sleep(1.5)

        # ── 5. Executar comando !tts ────────────────────────────────
        print()
        print("=" * 60)
        print("5. EXECUTANDO COMANDO !tts")
        print("=" * 60)
        print(f"   Frase: {frase}")

        tts_message = f"!tts {frase}"
        result = await handle_tts_command(tts_message, user_id, settings, db)
        print(f"   Resultado: {result}")

        # Give the poller time to pick up the newly concluded TTS.
        # The poller runs every poll_interval (1.0s), so 3s is plenty.
        print("   ⏳ Aguardando poller detectar o TTS concluído...")
        await asyncio.sleep(3.0)

        # ── 6. Verificar status no banco ────────────────────────────
        print()
        print("=" * 60)
        print("6. VERIFICANDO STATUS NO BANCO")
        print("=" * 60)

        # Busca o último TTS do usuário de teste
        last_time = await models.get_last_tts_time(db, user_id)
        if last_time:
            print(f"   ✅ Último TTS concluído em: {last_time}")
        else:
            print("   ⚠️ Nenhum TTS concluído encontrado para o usuário de teste")

        # Busca TTS pendentes para broadcast
        pending = await models.get_pending_tts(db, limit=5)
        print(f"   📋 TTS pendentes de broadcast: {len(pending)}")
        for p in pending:
            print(f"      id={p['id']} autor={p.get('autor')} texto={str(p.get('texto_falado'))[:50]}...")

        # ── 7. Aguardar broadcast via WebSocket ─────────────────────
        print()
        print("=" * 60)
        print("7. AGUARDANDO BROADCAST WEBSOCKET (até 20s)")
        print("=" * 60)

        # If we already got messages, great. Otherwise wait a bit more.
        if not received_messages:
            try:
                # Wait for at least one message, or timeout
                for _ in range(20):
                    if received_messages:
                        break
                    await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass

        # Cancel the listener task
        if not ws_task.done():
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

        if received_messages:
            print(f"   ✅ {len(received_messages)} mensagem(ns) recebida(s) via WebSocket")
        else:
            print("   ❌ Nenhuma mensagem recebida via WebSocket")

        # ── 8. Testar endpoint HTTP /tts-test ────────────────────────
        print()
        print("=" * 60)
        print("8. TESTANDO ENDPOINT HTTP /tts-test")
        print("=" * 60)
        http_tts_ok = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{ws_port}/tts-test",
                    params={"text": "Teste HTTP direto do endpoint TTS!"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    print(f"   Status: {resp.status}")
                    print(f"   Resposta: {json.dumps(data, indent=2)}")
                    http_tts_ok = data.get("ok") is True
        except Exception as exc:
            print(f"   ❌ Erro no endpoint /tts-test: {exc}")

        # ── 9. Testar endpoint HTTP /pending-tts ─────────────────────
        print()
        print("=" * 60)
        print("9. TESTANDO ENDPOINT HTTP /pending-tts")
        print("=" * 60)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{ws_port}/pending-tts",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    print(f"   Status: {resp.status}")
                    print(f"   Clientes conectados: {data.get('clients_connected')}")
                    print(f"   TTS pendentes: {data.get('count')}")
        except Exception as exc:
            print(f"   ❌ Erro no endpoint /pending-tts: {exc}")

        # ── 10. Resumo final ────────────────────────────────────────
        print()
        print("=" * 60)
        print("RESUMO DO TESTE")
        print("=" * 60)

        checks = []

        # Check 1: handle_tts_command retornou algo
        checks.append(("Comando !tts processado", result is not None and "Audio TTS gerado" in str(result) or "Falha" not in str(result)))

        # Check 2: TTS foi salvo no banco
        checks.append(("TTS registrado no banco", last_time is not None))

        # Check 3: Broadcast WebSocket funcionou
        checks.append(("Broadcast WebSocket", len(received_messages) > 0))

        # Check 4: URL do Catbox é válida
        has_valid_url = any(
            m.get("audio", "").startswith("https://files.catbox.moe/")
            for m in received_messages
        )
        checks.append(("URL do Catbox válida", has_valid_url or len(received_messages) == 0))

        # Check 5: Endpoint HTTP /tts-test
        checks.append(("Endpoint HTTP /tts-test", http_tts_ok))

        all_pass = True
        for name, passed in checks:
            icon = "✅" if passed else "❌"
            if not passed:
                all_pass = False
            print(f"   {icon} {name}")

        print()
        if all_pass:
            print("🎉 TODOS OS TESTES PASSARAM!")
        else:
            print("⚠️  Alguns testes falharam. Verifique os logs acima.")

    except Exception as exc:
        print(f"\n❌ ERRO: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        # ── Cleanup ─────────────────────────────────────────────────
        print()
        print("=" * 60)
        print("LIMPANDO...")
        print("=" * 60)
        try:
            await ws_server.stop()
            print("   ✅ WebSocket server parado")
        except Exception:
            pass
        try:
            await db.close()
            print("   ✅ Conexão com banco fechada")
        except Exception:
            pass
        print("   ✅ Teste concluído")


if __name__ == "__main__":
    asyncio.run(main())
