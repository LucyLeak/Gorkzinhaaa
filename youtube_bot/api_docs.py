from __future__ import annotations


API_DOCS_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gorkzinhaaa API</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--surface:#141d35;--border:#29385c;--text:#e8eefc;--muted:#9eadd0;--accent:#73a7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui,sans-serif;line-height:1.55}
main{max-width:1000px;margin:auto;padding:32px 20px}h1,h2{color:var(--accent)}h1{font-size:32px}
section{background:var(--surface);padding:20px;border:1px solid var(--border);border-radius:14px;margin:18px 0}
code,pre{background:#080d19;border-radius:8px}code{padding:2px 5px}pre{padding:14px;overflow:auto;white-space:pre-wrap}
.sample{position:relative;margin:10px 0}.copy{position:absolute;right:8px;top:8px;background:var(--accent);color:#07101f;border:0;border-radius:7px;padding:6px 10px;cursor:pointer}
.endpoint{color:#91d7ac;font-family:ui-monospace,monospace;font-weight:700}.muted{color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid var(--border);text-align:left}
button{background:var(--accent);border:0;border-radius:7px;padding:8px 12px;cursor:pointer;color:#07101f}
</style>
</head>
<body>
<main>
<h1>Gorkzinhaaa API</h1>
<p>API pública para gerar TTS, acompanhar solicitações e receber eventos em tempo real.</p>
<p>Base URL: <code id="base"></code></p>

<section>
<h2>Autenticação</h2>
<p>Crie uma chave na aba <strong>API Clients</strong> do painel administrativo. A chave é exibida uma única vez.</p>
<p>REST: envie <code>Authorization: Bearer API_KEY</code>. No WebSocket, use <code>?key=API_KEY</code>.</p>
<p>Escopos: <code>tts:generate</code>, <code>tts:subscribe</code>, <code>status:read</code>.</p>
</section>

<section>
<h2>POST /api/v1/tts</h2>
<p class="endpoint">POST /api/v1/tts</p>
<p>Escopo: <code>tts:generate</code>. Por padrão retorna <code>202</code> e continua em background. Use <code>?wait=true</code> ou <code>Prefer: wait=15</code> para aguardar.</p>
<div class="sample"><button class="copy" data-copy="curl">Copiar</button><pre id="curl">curl -X POST "$BASE/api/v1/tts" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text":"Olá do bot","provider":"gtts","voice":"pt"}'</pre></div>
<div class="sample"><button class="copy" data-copy="js">Copiar</button><pre id="js">const response = await fetch(`${BASE}/api/v1/tts?wait=true`, {
  method: "POST",
  headers: {"Authorization": "Bearer API_KEY", "Content-Type": "application/json"},
  body: JSON.stringify({text: "Olá do bot", provider: "gtts", voice: "pt"})
});
console.log(await response.json());</pre></div>
<div class="sample"><button class="copy" data-copy="py">Copiar</button><pre id="py">import aiohttp

BASE = "https://seu-host"
async with aiohttp.ClientSession() as session:
    async with session.post(
        f"{BASE}/api/v1/tts?wait=true",
        headers={"Authorization": "Bearer API_KEY", "Prefer": "wait=15"},
        json={"text": "Olá do bot", "provider": "gtts", "voice": "pt"},
    ) as response:
        print(await response.json())</pre></div>
<p>Resposta assíncrona: <code>{"id":123,"status":"processando","poll_url":"/api/v1/tts/123"}</code>.</p>
<p>Resposta concluída: <code>{"id":123,"status":"concluido","audio_url":"https://..."}</code>.</p>
</section>

<section>
<h2>Consulta e status</h2>
<p class="endpoint">GET /api/v1/tts/{id}</p>
<p>Escopo: <code>tts:generate</code> ou <code>status:read</code>.</p>
<div class="sample"><button class="copy" data-copy="get">Copiar</button><pre id="get">curl "$BASE/api/v1/tts/123" -H "Authorization: Bearer API_KEY"</pre></div>
<p class="endpoint">GET /api/v1/status</p>
<p>Escopo: <code>status:read</code>. Retorna <code>uptime</code>, <code>live_connected</code>, <code>pending_tts</code> e <code>last_live_video_id</code>.</p>
<p class="endpoint">GET /api/v1/health</p>
<p>Público, sem autenticação. Retorna <code>{"ok":true}</code>.</p>
</section>

<section>
<h2>WebSocket de eventos</h2>
<p>Conecte em <code>wss://SEU_HOST/api/v1/ws?key=SUA_CHAVE</code> com o escopo <code>tts:subscribe</code>. São permitidas até 5 conexões por chave.</p>
<p>Primeira mensagem: <code>{"type":"hello","client":"nome"}</code>.</p>
<p>Conclusão de TTS: <code>{"type":"tts","id":123,"username":"...","message":"...","audio":"https://...","created_at":"..."}</code>.</p>
</section>

<section>
<h2>Escopos e limites</h2>
<table><tr><th>Escopo</th><th>Acesso</th></tr>
<tr><td><code>tts:generate</code></td><td>Criar e consultar TTS</td></tr>
<tr><td><code>tts:subscribe</code></td><td>Eventos do WebSocket público</td></tr>
<tr><td><code>status:read</code></td><td>Status e consulta de TTS</td></tr></table>
<p class="muted">O limite de esperas síncronas é configurado por <code>API_TTS_MAX_CONCURRENT</code>. O timeout padrão é 15 segundos. Registros TTS da API são retidos por <code>TTS_API_RETENTION_HOURS</code> horas; depois disso, a consulta pode retornar 404. CORS usa <code>API_ALLOWED_ORIGINS</code>. REST: 10 POST TTS/min, 60 consultas TTS/min e 30 consultas de status/min por chave; respostas 429 incluem <code>Retry-After</code>.</p>
</section>

<section>
<h2>Testar health</h2>
<button id="health">Testar /api/v1/health</button><pre id="result"></pre>
</section>

<p><a href="/admin">Voltar ao painel administrativo</a></p>
</main>
<script>
const BASE = location.origin;
document.getElementById("base").textContent = BASE;
document.querySelectorAll(".copy").forEach(button => button.addEventListener("click", async () => {
  await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent.replaceAll("$BASE", BASE));
  button.textContent = "Copiado";
  setTimeout(() => button.textContent = "Copiar", 1200);
}));
document.getElementById("health").addEventListener("click", async () => {
  const response = await fetch(`${BASE}/api/v1/health`);
  document.getElementById("result").textContent = JSON.stringify(await response.json(), null, 2);
});
</script>
</body>
</html>"""
