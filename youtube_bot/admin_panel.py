"""Admin web panel — runtime settings editor, TTS approval queue, audio cleanup.

Served at /admin — protected by ADMIN_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from youtube_bot.db import models
from youtube_bot.db.pool import Database
from youtube_bot.fun.audio_cleanup import cleanup_audio_files

if TYPE_CHECKING:
    from youtube_bot.config import Settings

logger = logging.getLogger(__name__)

# ── Editable settings (whitelist) ────────────────────────────────────
# Only these keys can be changed at runtime via the admin panel.
# They map to os.environ keys.
EDITABLE_SETTINGS: dict[str, dict] = {
    "openai_api_key":       {"env": "OPENAI_API_KEY",       "label": "OpenAI API Key",       "type": "password"},
    "openai_base_url":      {"env": "OPENAI_BASE_URL",      "label": "OpenAI Base URL",      "type": "text"},
    "openai_chat_model":    {"env": "OPENAI_CHAT_MODEL",    "label": "Chat Model",           "type": "text"},
    "youtube_live_url":     {"env": "YOUTUBE_LIVE_URL",     "label": "YouTube Live URL",     "type": "text"},
    "youtube_channel_handle":{"env": "YOUTUBE_CHANNEL_HANDLE","label": "YouTube @handle",    "type": "text"},
    "youtube_api_key":      {"env": "YOUTUBE_API_KEY",      "label": "YouTube API Key",      "type": "password"},
    "giphy_api_key":        {"env": "GIPHY_API_KEY",        "label": "Giphy API Key",        "type": "password"},
    "tts_provider":         {"env": "TTS_PROVIDER",         "label": "TTS Provider",         "type": "select", "options": ["gtts", "openai"]},
    "tts_voice":            {"env": "TTS_VOICE",            "label": "TTS Voice",            "type": "text"},
    "tts_cooldown_minutes": {"env": "TTS_COOLDOWN_MINUTES", "label": "TTS Cooldown (min)",   "type": "number"},
    "dry_run":              {"env": "DRY_RUN",              "label": "Dry Run",              "type": "select", "options": ["true", "false"]},
    "poll_interval_seconds": {"env": "POLL_INTERVAL_SECONDS","label": "Poll Interval (s)",   "type": "number"},
    "max_repair_attempts":  {"env": "MAX_REPAIR_ATTEMPTS",  "label": "Max Repair Attempts",  "type": "number"},
    "coherence_threshold":  {"env": "COHERENCE_THRESHOLD",  "label": "Coherence Threshold",  "type": "number"},
    "brain_surprise_chance": {"env": "BRAIN_SURPRISE_CHANCE","label": "Brain Surprise %",    "type": "number"},
    "log_level":            {"env": "LOG_LEVEL",            "label": "Log Level",            "type": "select", "options": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    "public_base_url":      {"env": "PUBLIC_BASE_URL",      "label": "Public Base URL",      "type": "text"},
    "memory_retention_days":{"env": "MEMORY_RETENTION_DAYS","label": "Memory Retention (d)", "type": "number"},
}

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
    <button class="tab active" data-panel="settings">⚙️ Configurações</button>
    <button class="tab" data-panel="tts">🎙️ Fila TTS</button>
    <button class="tab" data-panel="cleanup">🗑️ Limpeza</button>
    <button class="tab" data-panel="terminal">💻 Terminal</button>
  </div>

  <!-- SETTINGS PANEL -->
  <div class="panel active" id="panel-settings">
    <div class="card">
      <h2>Variáveis de Ambiente (Runtime)</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Alterações aplicam imediatamente no os.environ. Requer restart do bot para algumas variáveis.</p>
      <form id="settingsForm"></form>
      <button class="btn primary" onclick="saveSettings()" style="margin-top:12px">💾 Salvar Todas</button>
    </div>
  </div>

  <!-- TTS QUEUE PANEL -->
  <div class="panel" id="panel-tts">
    <div class="card">
      <h2>Fila de Aprovação TTS</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:12px">Áudios pendentes de aprovação. Aprove ou rejeite cada um.</p>
      <div id="ttsQueue"></div>
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
    if (t.dataset.panel === 'tts') loadTTSQueue();
    if (t.dataset.panel === 'cleanup') loadAudioStats();
  });
});

// ── Settings ──────────────────────────────────────────
function buildSettingsForm(settings) {
  const form = document.getElementById('settingsForm');
  form.innerHTML = '';
  for (const [key, cfg] of Object.entries(settings)) {
    const div = document.createElement('div');
    div.className = 'form-group';
    const label = document.createElement('label');
    label.textContent = cfg.label;
    div.appendChild(label);

    if (cfg.type === 'select') {
      const sel = document.createElement('select');
      sel.name = key;
      for (const opt of (cfg.options || [])) {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        if (opt === cfg.current) o.selected = true;
        sel.appendChild(o);
      }
      div.appendChild(sel);
    } else {
      const input = document.createElement('input');
      input.type = cfg.type === 'password' ? 'password' : 'text';
      if (cfg.type === 'number') input.type = 'number';
      input.name = key;
      input.value = cfg.current || '';
      div.appendChild(input);
    }
    form.appendChild(div);
  }
}

function saveSettings() {
  const form = document.getElementById('settingsForm');
  const data = {};
  for (const el of form.elements) {
    if (el.name) data[el.name] = el.value;
  }
  api('/settings', {method: 'PUT', body: JSON.stringify(data)})
    .then(r => { if (r.ok) toast('Configurações salvas! ✅'); else toast('Erro: ' + (r.error || 'desconhecido'), 'error'); });
}

// ── TTS Queue ─────────────────────────────────────────
function loadTTSQueue() {
  api('/tts-queue').then(r => {
    const div = document.getElementById('ttsQueue');
    if (!r.items || !r.items.length) {
      div.innerHTML = '<p style="color:var(--muted)">Nenhum TTS pendente de aprovação.</p>';
      return;
    }
    let html = '<table><tr><th>ID</th><th>Usuário</th><th>Texto</th><th>Áudio</th><th>Status</th><th>Ações</th></tr>';
    for (const item of r.items) {
      html += `<tr>
        <td>${item.id}</td>
        <td>${item.username || '-'}</td>
        <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${item.texto_falado || ''}">${item.texto_falado || '-'}</td>
        <td class="audio-cell">${item.audio_url ? `<audio controls src="${item.audio_url}"></audio>` : '-'}</td>
        <td><span class="badge ${item.aprovado === null ? 'pending' : item.aprovado ? 'approved' : 'rejected'}">${item.aprovado === null ? 'pendente' : item.aprovado ? 'aprovado' : 'rejeitado'}</span></td>
        <td>
          ${item.aprovado === null ? `
            <button class="btn small primary" onclick="approveTTS(${item.id})">✅</button>
            <button class="btn small danger" onclick="rejectTTS(${item.id})">❌</button>
          ` : '-'}
        </td>
      </tr>`;
    }
    html += '</table>';
    div.innerHTML = html;
  });
}

function approveTTS(id) { api('/tts-queue/' + id, {method: 'PUT', body: JSON.stringify({aprovado: true})}).then(r => { toast(r.ok ? 'Aprovado!' : 'Erro', r.ok ? 'success' : 'error'); loadTTSQueue(); }); }
function rejectTTS(id) { api('/tts-queue/' + id, {method: 'PUT', body: JSON.stringify({aprovado: false})}).then(r => { toast(r.ok ? 'Rejeitado!' : 'Erro', r.ok ? 'success' : 'error'); loadTTSQueue(); }); }

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
api('/settings').then(r => { if (r.settings) buildSettingsForm(r.settings); });
api('/ping').then(r => {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  if (r.ok) { dot.className = 'status-dot online'; txt.textContent = 'Conectado'; }
  else { dot.className = 'status-dot offline'; txt.textContent = 'Offline'; }
});
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
    ) -> None:
        self.db = db
        self.settings = settings
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

    # ── API: Settings ──────────────────────────────────────────────

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        result = {}
        for key, cfg in EDITABLE_SETTINGS.items():
            result[key] = {
                "label": cfg["label"],
                "type": cfg["type"],
                "current": os.getenv(cfg["env"], ""),
            }
            if "options" in cfg:
                result[key]["options"] = cfg["options"]
        return web.json_response({"settings": result})

    async def handle_put_settings(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        updated = []
        for key, value in data.items():
            if key in EDITABLE_SETTINGS:
                env_key = EDITABLE_SETTINGS[key]["env"]
                os.environ[env_key] = str(value)
                updated.append(key)
                logger.info("Admin: set %s=%s", env_key, str(value)[:50])

        return web.json_response({"ok": True, "updated": updated})

    # ── API: TTS Queue ─────────────────────────────────────────────

    async def handle_tts_queue(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        rows = await self.db.fetch(
            """
            SELECT t.id, t.texto_original, t.texto_falado, t.audio_url, t.status,
                   t.aprovado, u.nome AS username
            FROM tts_solicitacoes t
            JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.status = 'concluido'
            ORDER BY t.criado_em DESC
            LIMIT 50
            """
        )
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "texto_original": r["texto_original"],
                "texto_falado": r["texto_falado"],
                "audio_url": r["audio_url"],
                "status": r["status"],
                "aprovado": r["aprovado"],
                "username": r["username"],
            })
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

        await self.db.execute(
            "UPDATE tts_solicitacoes SET aprovado = $1 WHERE id = $2",
            aprovado, tts_id_int,
        )
        logger.info("Admin: TTS #%d %s", tts_id_int, "aprovado" if aprovado else "rejeitado")
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
        app.router.add_get("/admin/api/settings", self.handle_get_settings)
        app.router.add_put("/admin/api/settings", self.handle_put_settings)
        app.router.add_get("/admin/api/tts-queue", self.handle_tts_queue)
        app.router.add_put("/admin/api/tts-queue/{id}", self.handle_tts_approve)
        app.router.add_get("/admin/api/audio-stats", self.handle_audio_stats)
        app.router.add_post("/admin/api/cleanup", self.handle_cleanup)
        app.router.add_post("/admin/api/terminal", self.handle_terminal)
        logger.info("Admin panel routes registered at /admin")
