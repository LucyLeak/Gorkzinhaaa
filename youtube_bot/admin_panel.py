"""Admin web panel — TTS approval queue, audio cleanup, and test terminal.

Served at /admin — protected by ADMIN_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.fun.audio_cleanup import cleanup_audio_files

if TYPE_CHECKING:
    from youtube_bot.config import Settings

logger = logging.getLogger(__name__)

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gorkzinhaaa — Admin Panel</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d2991d;
  --radius: 6px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; }
header h1 { font-size: 18px; color: var(--accent); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.status-dot.online { background: var(--green); }
.status-dot.offline { background: var(--red); }
main { max-width: 1100px; margin: 0 auto; padding: 20px; }
.tabs { display: flex; gap: 2px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
.tab { padding: 10px 20px; cursor: pointer; border: none; background: none; color: var(--muted); font-size: 14px; border-bottom: 2px solid transparent; transition: .2s; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.panel { display: none; }
.panel.active { display: block; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; margin-bottom: 16px; }
.card h2 { font-size: 15px; margin-bottom: 12px; color: var(--accent); }
.form-group { margin-bottom: 10px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.form-group label { min-width: 160px; font-size: 13px; color: var(--muted); }
.form-group input, .form-group select { flex: 1; min-width: 200px; padding: 6px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text); font-size: 13px; }
.form-group input[type="password"] { font-family: monospace; }
.btn { padding: 8px 16px; border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; font-size: 13px; background: var(--surface); color: var(--text); transition: .2s; }
.btn:hover { border-color: var(--accent); }
.btn.primary { background: #238636; border-color: #238636; color: #fff; }
.btn.primary:hover { background: #2ea043; }
.btn.danger { background: #da3633; border-color: #da3633; color: #fff; }
.btn.danger:hover { background: #f85149; }
.btn.small { padding: 4px 10px; font-size: 12px; }
.toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; border-radius: var(--radius); font-size: 13px; z-index: 999; animation: slideIn .3s; }
.toast.success { background: #238636; color: #fff; }
.toast.error { background: #da3633; color: #fff; }
@keyframes slideIn { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; }
.audio-cell audio { height: 28px; }
.queue-scroll { max-height: 520px; overflow: auto; }
.terminal-output { min-height: 180px; max-height: 360px; overflow: auto; background: #090d12; color: #9fef9f; white-space: pre-wrap; }
.terminal-output .error { color: var(--red); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge.pending { background: #1f3a5f; color: var(--accent); }
.badge.approved { background: #1a3a1a; color: var(--green); }
.badge.rejected { background: #3a1a1a; color: var(--red); }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; text-align: center; }
.stat .value { font-size: 24px; font-weight: 700; color: var(--accent); }
.stat .label { font-size: 12px; color: var(--muted); margin-top: 4px; }
textarea { width: 100%; min-height: 200px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text); padding: 10px; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px; resize: vertical; }
</style>
</head>
<body>
<header>
  <h1>🤖 Gorkzinhaaa Admin</h1>
  <span><span class="status-dot online" id="statusDot"></span><span id="statusText">Conectado</span></span>
</header>
<main>
  <div class="tabs">
    <button class="tab active" data-panel="tts">🎙️ Fila TTS</button>
    <button class="tab" data-panel="tts-test">🧪 Teste TTS</button>
    <button class="tab" data-panel="cleanup">🗑️ Limpeza</button>
    <button class="tab" data-panel="terminal">💻 Terminal</button>
  </div>

  <!-- TTS QUEUE PANEL -->
  <div class="panel" id="panel-tts">
    <div class="card">
      <h2>Fila de Aprovação TTS</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Áudios pendentes de aprovação. Aprove ou rejeite cada um.</p>
      <div id="ttsQueue" class="queue-scroll"></div>
    </div>
  </div>

  <!-- TTS TEST PANEL -->
  <div class="panel" id="panel-tts-test">
    <div class="card">
      <h2>Terminal de Teste TTS</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Gera um áudio isolado. Não cria item na fila nem interfere no bot.</p>
      <div class="form-group"><label for="ttsTestProvider">Provedor</label><select id="ttsTestProvider"><option>gtts</option><option>edge</option><option>openai</option><option>elevenlabs</option></select></div>
      <div class="form-group"><label for="ttsTestVoice">Voz / idioma</label><input id="ttsTestVoice" value="pt" placeholder="pt, pt-BR-FranciscaNeural, nova..."></div>
      <div class="form-group"><label for="ttsTestElevenVoice">ElevenLabs Voice ID</label><input id="ttsTestElevenVoice" placeholder="Opcional"></div>
      <div class="form-group"><label for="ttsTestModel">Modelo ElevenLabs</label><input id="ttsTestModel" placeholder="eleven_flash_v2_5"></div>
      <div class="form-group"><label for="ttsTestFormat">Formato ElevenLabs</label><input id="ttsTestFormat" placeholder="mp3_44100_128"></div>
      <textarea id="ttsTestText" maxlength="300" placeholder="Digite o texto para sintetizar..."></textarea>
      <button class="btn primary" onclick="runTtsTest()" style="margin-top:10px">▶️ Gerar áudio</button>
      <pre id="ttsTestOutput" class="terminal-output" style="margin-top:12px"></pre>
      <audio id="ttsTestAudio" controls style="display:none;width:100%;margin-top:10px"></audio>
    </div>
  </div>

  <!-- CLEANUP PANEL -->
  <div class="panel" id="panel-cleanup">
    <div class="card">
      <h2>Limpeza de Áudios</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Remove arquivos de áudio antigos, priorizando os maiores.</p>
      <div class="stats-grid" id="audioStats"></div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn" onclick="runCleanup(false)">🔍 Simular (Dry Run)</button>
        <button class="btn danger" onclick="runCleanup(true)">🗑️ Executar Limpeza</button>
      </div>
      <pre id="cleanupResult" style="margin-top:12px;font-size:12px;color:var(--muted)"></pre>
    </div>
  </div>

  <!-- TERMINAL PANEL -->
  <div class="panel" id="panel-terminal">
    <div class="card">
      <h2>Terminal / Shell</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Execute comandos SQL e operações administrativas.</p>
      <textarea id="sqlInput" placeholder="SELECT * FROM tts_solicitacoes ORDER BY criado_em DESC LIMIT 10;"></textarea>
      <div style="display:flex;gap:10px;margin-top:10px">
        <button class="btn primary" onclick="runSQL()">▶️ Executar</button>
        <select id="quickQuery" onchange="document.getElementById('sqlInput').value=this.value" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:var(--radius);font-size:13px">
          <option value="">— Queries rápidas —</option>
          <option value="SELECT status, COUNT(*) FROM tts_solicitacoes GROUP BY status ORDER BY status">TTS por status</option>
          <option value="SELECT u.nome, u.total_interacoes, u.pontos FROM usuarios u ORDER BY u.total_interacoes DESC LIMIT 20">Top usuários</option>
          <option value="SELECT cerebro_utilizado, COUNT(*), SUM(CASE WHEN aprovada THEN 1 ELSE 0 END) as aprovadas FROM respostas_geradas GROUP BY cerebro_utilizado">Stats cérebros</option>
          <option value="SELECT tipo, COUNT(*) FROM memorias_semanticas GROUP BY tipo">Memórias por tipo</option>
          <option value="SELECT pg_size_pretty(pg_database_size(current_database())) as db_size">Tamanho do banco</option>
        </select>
      </div>
      <pre id="sqlResult" style="margin-top:12px;font-size:12px;max-height:400px;overflow:auto;background:var(--bg);padding:10px;border-radius:var(--radius)"></pre>
    </div>
  </div>
</main>
<div id="toastContainer"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
if (!TOKEN) { document.body.innerHTML = '<div style="padding:40px;text-align:center"><h2>Acesso Restrito</h2><p>Adicione ?token=SEU_TOKEN na URL.</p></div>'; }

function api(path, opts={}) {
  const url = '/admin/api' + path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  return fetch(url, { headers: {'Content-Type': 'application/json'}, ...opts })
    .then(r => r.json().then(d => ({status: r.status, ...d})))
    .catch(e => ({error: e.message}));
}

function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  document.getElementById('toastContainer').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ── Tabs ──────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.panel).classList.add('active');
    if (t.dataset.panel === 'cleanup') loadAudioStats();
  });
});

// ── TTS Queue ─────────────────────────────────────────
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderTTSQueue(items) {
  const div = document.getElementById('ttsQueue');
  const atBottom = div.scrollHeight - div.scrollTop - div.clientHeight < 24;
  if (!items || !items.length) {
    div.innerHTML = '<p style="color:var(--muted)">Nenhum TTS concluído na fila.</p>';
    return;
  }
  let html = '<table><tr><th>ID</th><th>Usuário</th><th>Texto</th><th>Áudio</th><th>Status</th><th>Ações</th></tr>';
  for (const item of items) {
    const state = item.aprovado === null ? 'pendente' : item.aprovado ? 'aprovado' : 'rejeitado';
    html += `<tr>
      <td>${item.id}</td>
      <td>${escapeHtml(item.username || '-')}</td>
      <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(item.texto_falado || '')}">${escapeHtml(item.texto_falado || '-')}</td>
      <td class="audio-cell">${item.audio_url ? `<audio controls src="${escapeHtml(item.audio_url)}"></audio>` : '-'}</td>
      <td><span class="badge ${state === 'pendente' ? 'pending' : state === 'aprovado' ? 'approved' : 'rejected'}">${state}</span></td>
      <td>${item.aprovado === null ? `<button class="btn small primary" onclick="approveTTS(${item.id})">✅</button> <button class="btn small danger" onclick="rejectTTS(${item.id})">❌</button>` : '-'}</td>
    </tr>`;
  }
  div.innerHTML = html + '</table>';
  if (atBottom) div.scrollTop = div.scrollHeight;
}

function approveTTS(id) { api('/tts-queue/' + id, {method: 'PUT', body: JSON.stringify({aprovado: true})}).then(r => toast(r.ok ? 'Aprovado!' : 'Erro', r.ok ? 'success' : 'error')); }
function rejectTTS(id) { api('/tts-queue/' + id, {method: 'PUT', body: JSON.stringify({aprovado: false})}).then(r => toast(r.ok ? 'Rejeitado!' : 'Erro', r.ok ? 'success' : 'error')); }

let adminSocket;
function connectAdminSocket() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  adminSocket = new WebSocket(`${scheme}://${location.host}/admin/ws?token=${encodeURIComponent(TOKEN)}`);
  adminSocket.onopen = () => { document.getElementById('statusText').textContent = 'Conectado'; };
  adminSocket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === 'tts_queue') renderTTSQueue(message.items);
    if (message.type === 'tts_test_progress') appendTtsTestLog(message.message, message.error);
    if (message.type === 'tts_test_result') showTtsTestResult(message);
  };
  adminSocket.onclose = () => {
    document.getElementById('statusText').textContent = 'Reconectando...';
    setTimeout(connectAdminSocket, 1000);
  };
}

function appendTtsTestLog(message, error=false) {
  const output = document.getElementById('ttsTestOutput');
  output.classList.toggle('error', Boolean(error));
  output.textContent += (output.textContent ? '\n' : '') + message;
  output.scrollTop = output.scrollHeight;
}

function showTtsTestResult(message) {
  if (message.error) appendTtsTestLog(message.error, true);
  if (message.audio_url) {
    appendTtsTestLog('Concluído: ' + message.audio_url);
    const audio = document.getElementById('ttsTestAudio');
    audio.src = message.audio_url;
    audio.style.display = 'block';
  }
}

function runTtsTest() {
  const text = document.getElementById('ttsTestText').value.trim();
  if (!text) return toast('Digite um texto.', 'error');
  const payload = {
    type: 'tts_test',
    text,
    provider: document.getElementById('ttsTestProvider').value,
    voice: document.getElementById('ttsTestVoice').value.trim(),
    elevenlabs_voice_id: document.getElementById('ttsTestElevenVoice').value.trim(),
    elevenlabs_model_id: document.getElementById('ttsTestModel').value.trim(),
    elevenlabs_output_format: document.getElementById('ttsTestFormat').value.trim(),
  };
  document.getElementById('ttsTestOutput').textContent = 'Solicitação:\n' + JSON.stringify(payload, null, 2);
  document.getElementById('ttsTestAudio').style.display = 'none';
  adminSocket.send(JSON.stringify(payload));
}

// ── Cleanup ───────────────────────────────────────────
function loadAudioStats() {
  api('/audio-stats').then(r => {
    const div = document.getElementById('audioStats');
    div.innerHTML = `
      <div class="stat"><div class="value">${r.total_files || 0}</div><div class="label">Arquivos</div></div>
      <div class="stat"><div class="value">${r.total_mb || 0} MB</div><div class="label">Tamanho Total</div></div>
      <div class="stat"><div class="value">${r.oldest_file || '-'}</div><div class="label">Arquivo mais antigo</div></div>
      <div class="stat"><div class="value">${r.largest_file || '-'}</div><div class="label">Maior arquivo</div></div>
    `;
  });
}

function runCleanup(execute) {
  document.getElementById('cleanupResult').textContent = 'Executando...';
  api('/cleanup', {method: 'POST', body: JSON.stringify({execute: execute})})
    .then(r => {
      document.getElementById('cleanupResult').textContent = JSON.stringify(r, null, 2);
      loadAudioStats();
      toast(r.execute ? 'Limpeza concluída!' : 'Simulação concluída!');
    });
}

// ── Terminal ──────────────────────────────────────────
function runSQL() {
  const sql = document.getElementById('sqlInput').value.trim();
  if (!sql) return;
  document.getElementById('sqlResult').textContent = 'Executando...';
  api('/terminal', {method: 'POST', body: JSON.stringify({sql: sql})})
    .then(r => {
      document.getElementById('sqlResult').textContent = JSON.stringify(r, null, 2);
    });
}

// ── Init ──────────────────────────────────────────────
api('/ping').then(r => {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  if (r.ok) { dot.className = 'status-dot online'; txt.textContent = 'Conectado'; }
  else { dot.className = 'status-dot offline'; txt.textContent = 'Offline'; }
});
connectAdminSocket();
</script>
</body>
</html>"""


class AdminPanel:
    """Admin web panel served at /admin on the existing aiohttp server."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        admin_token: str = "",
        on_tts_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.on_tts_changed = on_tts_changed
        self.admin_token = admin_token or os.getenv("ADMIN_TOKEN", "")
        if not self.admin_token:
            logger.warning("ADMIN_TOKEN not set — admin panel will be inaccessible!")

    def _check_auth(self, request: web.Request) -> bool:
        token = request.query.get("token", "")
        if not self.admin_token:
            return False
        return token == self.admin_token

    def _auth_error(self) -> web.Response:
        return web.json_response({"error": "Unauthorized"}, status=401)

    # ── Page ───────────────────────────────────────────────────────

    async def handle_page(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.Response(text="Unauthorized — add ?token=YOUR_TOKEN", status=401, content_type="text/plain")
        return web.Response(text=ADMIN_HTML, content_type="text/html")

    # ── API: Ping ──────────────────────────────────────────────────

    async def handle_ping(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        return web.json_response({"ok": True, "time": time.time()})

    # ── API: TTS Queue ─────────────────────────────────────────────

    async def get_tts_queue_items(self) -> list[dict[str, object]]:
        rows = await self.db.fetch(
            """
            SELECT t.id, t.texto_original, t.texto_falado, t.audio_url, t.status,
                   t.aprovado, u.nome AS username
            FROM tts_solicitacoes t
            JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.status = 'concluido'
            ORDER BY t.criado_em ASC
            LIMIT 50
            """
        )
        return [
            {
                "id": r["id"],
                "texto_original": r["texto_original"],
                "texto_falado": r["texto_falado"],
                "audio_url": r["audio_url"],
                "status": r["status"],
                "aprovado": r["aprovado"],
                "username": r["username"],
            }
            for r in rows
        ]

    async def handle_tts_queue(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        try:
            items = await self.get_tts_queue_items()
        except Exception:
            # Fallback: column 'aprovado' may not exist yet (pending migration)
            rows = await self.db.fetch(
                """
                SELECT t.id, t.texto_original, t.texto_falado, t.audio_url, t.status,
                       NULL AS aprovado, u.nome AS username
                FROM tts_solicitacoes t
                JOIN usuarios u ON u.id = t.usuario_id
                WHERE t.status = 'concluido'
                ORDER BY t.criado_em ASC
                LIMIT 50
                """
            )
            items = [
                {
                    "id": r["id"],
                    "texto_original": r["texto_original"],
                    "texto_falado": r["texto_falado"],
                    "audio_url": r["audio_url"],
                    "status": r["status"],
                    "aprovado": r["aprovado"],
                    "username": r["username"],
                }
                for r in rows
            ]
        return web.json_response({"items": items, "count": len(items)})

    async def handle_tts_approve(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        tts_id = request.match_info.get("id", "")
        try:
            data = await request.json()
            aprovado = data.get("aprovado", False)
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        try:
            tts_id_int = int(tts_id)
        except ValueError:
            return web.json_response({"error": "Invalid ID"}, status=400)

        try:
            await self.db.execute(
                "UPDATE tts_solicitacoes SET aprovado = $1 WHERE id = $2",
                aprovado, tts_id_int,
            )
        except Exception:
            return web.json_response(
                {"error": "Coluna 'aprovado' ainda não existe no banco. A migration será aplicada no próximo deploy."},
                status=500,
            )
        logger.info("Admin: TTS #%d %s", tts_id_int, "aprovado" if aprovado else "rejeitado")
        if self.on_tts_changed is not None:
            await self.on_tts_changed()
        return web.json_response({"ok": True, "id": tts_id_int, "aprovado": aprovado})

    # ── API: Audio Stats & Cleanup ─────────────────────────────────

    async def handle_audio_stats(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        from youtube_bot.fun.audio_cleanup import _get_audio_files

        audio_dir = Path(self.settings.tts_output_dir)
        files = _get_audio_files(audio_dir)
        total_bytes = sum(f[1] for f in files)
        oldest = files[-1] if files else None
        largest = files[0] if files else None

        return web.json_response({
            "total_files": len(files),
            "total_mb": round(total_bytes / 1e6, 2),
            "oldest_file": oldest[0].name if oldest else None,
            "oldest_age_hours": round((time.time() - oldest[2]) / 3600, 1) if oldest else None,
            "largest_file": largest[0].name if largest else None,
            "largest_mb": round(largest[1] / 1e6, 2) if largest else None,
        })

    async def handle_cleanup(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        try:
            data = await request.json()
            execute = data.get("execute", False)
        except Exception:
            execute = False

        result = cleanup_audio_files(
            audio_dir=self.settings.tts_output_dir,
            dry_run=not execute,
        )
        return web.json_response({**result, "execute": execute})

    # ── API: Terminal (SQL) ────────────────────────────────────────

    async def handle_terminal(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        try:
            data = await request.json()
            sql = (data.get("sql") or "").strip()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        if not sql:
            return web.json_response({"error": "Empty SQL"}, status=400)

        # Security: only allow SELECT, WITH (CTE), EXPLAIN
        sql_upper = sql.upper().strip()
        allowed = ("SELECT", "WITH", "EXPLAIN", "SHOW")
        if not any(sql_upper.startswith(prefix) for prefix in allowed):
            return web.json_response({"error": "Only SELECT/WITH/EXPLAIN/SHOW queries allowed"}, status=403)

        try:
            start = time.monotonic()
            rows = await self.db.fetch(sql)
            elapsed = round(time.monotonic() - start, 3)
            result = [dict(r) for r in rows]
            # Convert non-serializable types
            for row in result:
                for k, v in row.items():
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif isinstance(v, (bytes, memoryview)):
                        row[k] = f"<{len(v)} bytes>"
            return web.json_response({
                "rows": result[:200],  # limit to 200 rows
                "count": len(result),
                "truncated": len(result) > 200,
                "elapsed_ms": round(elapsed * 1000, 1),
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    # ── Register routes ────────────────────────────────────────────

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get("/admin", self.handle_page)
        app.router.add_get("/admin/api/ping", self.handle_ping)
        app.router.add_get("/admin/api/tts-queue", self.handle_tts_queue)
        app.router.add_put("/admin/api/tts-queue/{id}", self.handle_tts_approve)
        app.router.add_get("/admin/api/audio-stats", self.handle_audio_stats)
        app.router.add_post("/admin/api/cleanup", self.handle_cleanup)
        app.router.add_post("/admin/api/terminal", self.handle_terminal)
        logger.info("Admin panel routes registered at /admin")
