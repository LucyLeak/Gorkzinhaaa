import postgres from "postgres";

// ─── Config ───────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.TTS_BACKEND_PORT || "3000");
const DATABASE_URL = process.env.DATABASE_URL;
const POLL_INTERVAL_MS = parseInt(process.env.TTS_POLL_INTERVAL_MS || "2000");

if (!DATABASE_URL) {
  console.error("❌ DATABASE_URL not set. Copy .env.example or set the variable.");
  process.exit(1);
}

// ─── Database ─────────────────────────────────────────────────────────────────

const sql = postgres(DATABASE_URL, {
  max: 3,
  idle_timeout: 30,
  connect_timeout: 10,
});

// ─── WebSocket clients ────────────────────────────────────────────────────────

type WsClient = import("bun").ServerWebSocket<unknown>;
const clients = new Set<WsClient>();

// ─── Poller ───────────────────────────────────────────────────────────────────

async function pollTtsQueue(): Promise<void> {
  try {
    const rows = await sql<{
      id: number;
      texto_falado: string;
      audio_url: string;
      criado_em: string;
      autor: string;
    }[]>`
      SELECT t.id, t.texto_falado, t.audio_url, t.criado_em, u.nome AS autor
      FROM tts_solicitacoes t
      JOIN usuarios u ON u.id = t.usuario_id
      WHERE t.status = 'concluido'
      ORDER BY t.criado_em ASC
      LIMIT 10
    `;

    for (const row of rows) {
      // The Python bot always uploads to Catbox.moe and stores the HTTPS URL,
      // then deletes the local MP3 file. The WebSocket payload sends the
      // Catbox URL directly with type "url" so clients can fetch it.
      const payload = JSON.stringify({
        id: row.id,
        username: row.autor,
        message: row.texto_falado,
        type: "url",
        audio: row.audio_url,
      });

      // Broadcast to all connected WebSocket clients
      for (const client of clients) {
        try {
          client.send(payload);
        } catch {
          clients.delete(client);
        }
      }

      // Mark as reproduzido
      await sql`UPDATE tts_solicitacoes SET status = 'reproduzido' WHERE id = ${row.id}`;

      console.log(`📢 TTS #${row.id} broadcasted: "${row.texto_falado.slice(0, 60)}..."`);
    }
  } catch (err) {
    console.error("❌ Poll error:", err);
  }
}

// ─── HTTP + WebSocket server ──────────────────────────────────────────────────

Bun.serve({
  port: PORT,
  fetch(req, server) {
    const url = new URL(req.url);

    // Upgrade to WebSocket
    if (url.pathname === "/ws") {
      const upgraded = server.upgrade(req);
      if (!upgraded) {
        return new Response("WebSocket upgrade failed", { status: 426 });
      }
      return undefined;
    }

    // Health check
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", clients: clients.size }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("Gork TTS Backend", { status: 200 });
  },

  websocket: {
    open(ws) {
      clients.add(ws);
      console.log(`🔌 Client connected (total: ${clients.size})`);
    },
    close(ws) {
      clients.delete(ws);
      console.log(`🔌 Client disconnected (total: ${clients.size})`);
    },
    message(ws, msg) {
      // Clients can send ping, we ignore other messages
      const text = typeof msg === "string" ? msg : new TextDecoder().decode(msg);
      if (text === "ping") {
        ws.send("pong");
      }
    },
  },
});

console.log(`🚀 Gork TTS Backend running on http://localhost:${PORT}`);
console.log(`   WebSocket: ws://localhost:${PORT}/ws`);
console.log(`   Sending Catbox URLs (type="url") — no local audio serving`);
console.log(`   Polling every ${POLL_INTERVAL_MS}ms`);

// ─── Start poller ─────────────────────────────────────────────────────────────

setInterval(pollTtsQueue, POLL_INTERVAL_MS);

// Graceful shutdown
process.on("SIGINT", async () => {
  console.log("\n🛑 Shutting down...");
  await sql.end();
  process.exit(0);
});
