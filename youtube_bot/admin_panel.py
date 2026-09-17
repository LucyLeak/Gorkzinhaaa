"""Admin web panel — TTS approval queue, audio cleanup, and test terminal.

Served at /admin — protected by ADMIN_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
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

ADMIN_HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gorkzinhaaa ? Admin</title><style>
:root{--bg:#080c16;--surface:#111827;--surface2:#17233a;--line:#293956;--text:#e9efff;--muted:#9aa9c5;--accent:#76a9ff;--ok:#38d39f;--danger:#ff6b7a;--warn:#f7c96b;--shadow:0 16px 50px #0005} [data-theme=light]{--bg:#f3f6fc;--surface:#fff;--surface2:#edf3ff;--line:#d5deee;--text:#17223a;--muted:#5c6b85;--accent:#356ee8;--shadow:0 10px 30px #34507822}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,system-ui,sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}.shell{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{background:var(--surface);border-right:1px solid var(--line);padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-size:20px;font-weight:800;color:var(--accent);padding:0 12px 26px}.nav{display:grid;gap:5px}.nav button{border:0;background:transparent;color:var(--muted);text-align:left;padding:12px;border-radius:10px}.nav button:hover,.nav button.active{background:var(--surface2);color:var(--text)}.side-foot{position:absolute;bottom:20px;left:25px;color:var(--muted);font-size:12px}.side-foot a{color:var(--accent)}main{min-width:0}.top{height:70px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 28px;background:var(--surface)}.top h1{font-size:18px;margin:0}.status{color:var(--muted)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:7px}.content{max-width:1200px;padding:28px;margin:auto}.panel{display:none}.panel.active{display:block}.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:var(--shadow)}h2{font-size:16px;margin:0 0 8px}.muted{color:var(--muted);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.stat{background:var(--surface2);border-radius:11px;padding:15px}.stat b{display:block;font-size:23px;color:var(--accent)}label{display:block;color:var(--muted);font-size:12px;margin:12px 0 5px}input,select,textarea{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:100px;resize:vertical}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:var(--surface2);color:var(--text);padding:9px 14px;border-radius:8px}.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}.btn.danger{color:#fff;background:var(--danger);border-color:var(--danger)}.btn.small{padding:5px 9px;font-size:12px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px}.badge{border-radius:99px;padding:3px 9px;font-size:11px;background:var(--surface2)}.badge.ok{color:var(--ok)}.badge.bad{color:var(--danger)}.badge.wait{color:var(--warn)}pre{background:var(--bg);padding:14px;overflow:auto;border-radius:8px;min-height:80px}.toast{position:fixed;right:22px;bottom:22px;background:var(--surface2);border:1px solid var(--line);padding:12px 16px;border-radius:9px;box-shadow:var(--shadow);z-index:4}.mobile{display:none}@media(max-width:760px){.shell{display:block}.side{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line);padding:12px}.brand{padding:8px}.nav{display:flex;overflow:auto}.nav button{white-space:nowrap}.side-foot{display:none}.top{padding:0 16px}.content{padding:16px}.mobile{display:block}}
</style></head><body><div class="shell"><aside class="side"><div class="brand">? Gorkzinhaaa</div><nav class="nav" aria-label="Navegação"><button class="active" data-panel="tts">??? Fila TTS</button><button data-panel="tts-test">?? Teste TTS</button><button data-panel="personalidades">?? Personalidades</button><button data-panel="cleanup">?? Limpeza</button><button data-panel="terminal">? Terminal</button><button data-panel="clients">?? API Clients</button></nav><div class="side-foot"><a href="/docs">Documentação da API</a><br><a href="/">Início</a></div></aside><main><header class="top"><h1 id="title">Fila de aprovação</h1><div class="row"><button class="btn small" id="theme" aria-label="Alternar tema">? Tema</button><span class="status"><i class="dot" id="dot"></i><span id="statusText">Conectando</span></span></div></header><section class="content">
<section class="panel active" id="panel-tts"><div class="card"><h2>Fila de aprovação TTS</h2><p class="muted">Aprove ou rejeite os áudios produzidos pelo bot.</p><label><input type="checkbox" id="hideAdminTests" style="width:auto"> Ocultar testes administrativos</label><div class="table-wrap" id="ttsQueue"></div></div></section>
<section class="panel" id="panel-tts-test"><div class="card"><h2>Terminal de teste TTS</h2><p class="muted">Testes ficam no histórico e não interferem no processamento.</p><div class="grid"><div><label>Provedor</label><select id="ttsTestProvider"><option>gtts</option><option>edge</option><option>openai</option><option>elevenlabs</option></select></div><div><label>Voz / idioma</label><input id="ttsTestVoice" value="pt"></div></div><label>Texto</label><textarea id="ttsTestText" maxlength="300" placeholder="Digite o texto para sintetizar..."></textarea><button class="btn primary" onclick="runTtsTest()">Gerar Áudio</button><pre id="ttsTestOutput"></pre><audio id="ttsTestAudio" controls style="display:none;width:100%"></audio></div></section>
<section class="panel" id="panel-personalidades"><div class="card"><h2>Personalidades</h2><input id="personalitySearch" placeholder="Buscar por handle, nome ou channel ID"><p class="muted" id="personalityCounts"></p><div class="table-wrap" id="personalities"></div><button class="btn small" onclick="personalityPage=Math.max(0,personalityPage-1);loadPersonalities()">Anterior</button> <button class="btn small" onclick="personalityPage++;loadPersonalities()">Próxima</button></div></section>
<section class="panel" id="panel-cleanup"><div class="card"><h2>Limpeza de áudios</h2><p class="muted">Simule antes de remover arquivos antigos.</p><div class="grid" id="audioStats"></div><div class="row"><button class="btn" onclick="runCleanup(false)">Simular</button><button class="btn danger" onclick="runCleanup(true)">Executar limpeza</button></div><pre id="cleanupResult"></pre></div></section>
<section class="panel" id="panel-terminal"><div class="card"><h2>Terminal SQL</h2><p class="muted">Somente consultas de leitura são aceitas.</p><textarea id="sqlInput" placeholder="SELECT * FROM tts_solicitacoes LIMIT 10"></textarea><button class="btn primary" onclick="runSQL()">Executar</button><pre id="sqlResult"></pre></div></section>
<section class="panel" id="panel-clients"><div class="card"><h2>Clientes da API</h2><p class="muted">A chave completa aparece uma única vez. Guarde-a em local seguro.</p><div class="grid"><div><label>Nome do cliente</label><input id="clientName" placeholder="Meu aplicativo"></div><div><label>Escopos</label><div class="row"><label><input class="scope" type="checkbox" value="tts:generate" checked style="width:auto"> gerar</label><label><input class="scope" type="checkbox" value="tts:subscribe" style="width:auto"> eventos</label><label><input class="scope" type="checkbox" value="status:read" style="width:auto"> status</label></div></div></div><button class="btn primary" onclick="createClient()">Criar chave</button><pre id="newKey" hidden></pre><div class="table-wrap" id="clientsTable"></div></div></section>
</section></main></div><div id="toast" role="status" aria-live="polite"></div><script>
const ADMIN_TEST_USER_ID=__ADMIN_TEST_USER_ID__,TOKEN=new URLSearchParams(location.search).get('token')||'';let personalityPage=0,adminSocket;const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function api(path,opt={}){let u='/admin/api'+path+(path.includes('?')?'&':'?')+'token='+encodeURIComponent(TOKEN);return fetch(u,{headers:{'Content-Type':'application/json'},...opt}).then(async r=>({status:r.status,...await r.json()})).catch(e=>({error:e.message}))}function toast(s){document.getElementById('toast').textContent=s;setTimeout(()=>document.getElementById('toast').textContent='',3200)}
document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-panel]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active',x.id==='panel-'+b.dataset.panel));document.getElementById('title').textContent=b.textContent.replace(/^\S+\s/,'');if(b.dataset.panel==='cleanup')loadAudioStats();if(b.dataset.panel==='personalidades')loadPersonalities();if(b.dataset.panel==='clients')loadClients()});document.getElementById('theme').onclick=()=>{let t=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=t;localStorage.theme=t};document.documentElement.dataset.theme=localStorage.theme||'dark';
function loadPersonalities(){api('/user-personalities?search='+encodeURIComponent(document.getElementById('personalitySearch').value)+'&offset='+(personalityPage*50)).then(r=>{document.getElementById('personalityCounts').textContent=`Total: ${r.total||0} ? Página ${personalityPage+1}`;document.getElementById('personalities').innerHTML='<table><tr><th>Canal</th><th>Nome</th><th>Personalidade</th><th>Notas</th></tr>'+(r.items||[]).map(u=>`<tr><td>${esc(u.youtube_channel_id||'-')}</td><td>${esc(u.nome||u.youtube_id)}</td><td><select onchange="savePersonality('${esc(u.youtube_channel_id||'')}',this.value,this.parentElement.nextElementSibling.firstElementChild.value)"><option ${u.personalidade==='amigo'?'selected':''}>amigo</option><option ${!u.personalidade||u.personalidade==='neutro'?'selected':''}>neutro</option><option ${u.personalidade==='inimigo'?'selected':''}>inimigo</option><option ${u.personalidade==='evitar'?'selected':''}>evitar</option><option ${u.personalidade==='bloqueado'?'selected':''}>bloqueado</option></select></td><td><textarea rows="1">${esc(u.personalidade_notas||'')}</textarea></td></tr>`).join('')+'</table>'})}function savePersonality(c,p,n){if(!c)return toast('Canal sem ID');api('/user-personality',{method:'POST',body:JSON.stringify({channel_id:c,personalidade:p,notas:n})}).then(r=>toast(r.ok?'Salvo':'Erro'))}document.getElementById('personalitySearch').oninput=()=>{personalityPage=0;loadPersonalities()};
function renderTTSQueue(items){if(document.getElementById('hideAdminTests').checked)items=items.filter(x=>Number(x.usuario_id)!==ADMIN_TEST_USER_ID);document.getElementById('ttsQueue').innerHTML=items.length?'<table><tr><th>ID</th><th>Usuário</th><th>Texto</th><th>Áudio</th><th>Status</th><th>Ação</th></tr>'+items.map(x=>`<tr><td>${x.id}</td><td>${esc(x.username||'-')}</td><td>${esc(x.texto_falado)}</td><td>${x.audio_url?`<audio controls src="${esc(x.audio_url)}"></audio>`:'-'}</td><td><span class="badge ${x.aprovado===null?'wait':x.aprovado?'ok':'bad'}">${x.aprovado===null?'pendente':x.aprovado?'aprovado':'rejeitado'}</span></td><td>${x.aprovado===null?`<button class="btn small" onclick="approveTTS(${x.id},true)">Aprovar</button> <button class="btn small danger" onclick="approveTTS(${x.id},false)">Rejeitar</button>`:'-'}</td></tr>`).join('')+'</table>':'<p class="muted">Nenhum Áudio na fila.</p>'}function approveTTS(id,v){api('/tts-queue/'+id,{method:'PUT',body:JSON.stringify({aprovado:v})}).then(()=>toast(v?'Aprovado':'Rejeitado'))}
function connectAdminSocket(){let s=location.protocol==='https:'?'wss':'ws';adminSocket=new WebSocket(`${s}://${location.host}/admin/ws?token=${encodeURIComponent(TOKEN)}`);adminSocket.onopen=()=>{statusText.textContent='Conectado'};adminSocket.onmessage=e=>{let m=JSON.parse(e.data);if(m.type==='tts_queue')renderTTSQueue(m.items);if(m.type==='tts_test_progress')ttsTestOutput.textContent+=(ttsTestOutput.textContent?'\n':'')+m.message;if(m.type==='tts_test_result'&&m.audio_url){ttsTestAudio.src=m.audio_url;ttsTestAudio.style.display='block'}};adminSocket.onclose=()=>{statusText.textContent='Reconectando?';setTimeout(connectAdminSocket,1500)}}function runTtsTest(){let text=ttsTestText.value.trim();if(!text)return toast('Digite um texto');ttsTestOutput.textContent='Iniciando?';adminSocket.send(JSON.stringify({type:'tts_test',text,provider:ttsTestProvider.value,voice:ttsTestVoice.value}))}
function loadAudioStats(){api('/audio-stats').then(r=>audioStats.innerHTML=`<div class=stat><b>${r.total_files||0}</b>arquivos</div><div class=stat><b>${r.total_mb||0} MB</b>total</div><div class=stat><b>${r.oldest_file||'-'}</b>mais antigo</div>`)}function runCleanup(e){cleanupResult.textContent='Executando?';api('/cleanup',{method:'POST',body:JSON.stringify({execute:e})}).then(r=>{cleanupResult.textContent=JSON.stringify(r,null,2);loadAudioStats();toast('Concluído')})}function runSQL(){api('/terminal',{method:'POST',body:JSON.stringify({sql:sqlInput.value})}).then(r=>sqlResult.textContent=JSON.stringify(r,null,2))}
function loadClients(){api('/api-clients').then(r=>clientsTable.innerHTML='<table><tr><th>Nome</th><th>Escopos</th><th>Criada</th><th>Status</th><th></th></tr>'+(r.items||[]).map(x=>`<tr><td>${esc(x.name)}</td><td>${esc((x.scopes||[]).join(', '))}</td><td>${esc(x.created_at)}</td><td><span class="badge ${x.revoked_at?'bad':'ok'}">${x.revoked_at?'revogada':'ativa'}</span></td><td>${x.revoked_at?'-':`<button class="btn small danger" onclick="revokeClient(${x.id})">Revogar</button>`}</td></tr>`).join('')+'</table>')}function createClient(){let scopes=[...document.querySelectorAll('.scope:checked')].map(x=>x.value);api('/api-clients',{method:'POST',body:JSON.stringify({name:clientName.value,scopes})}).then(r=>{if(r.key){newKey.hidden=false;newKey.textContent='Chave (copie agora): '+r.key;loadClients()}toast(r.key?'Chave criada':'Erro')})}function revokeClient(id){if(confirm('Revogar esta chave?'))api('/api-clients/'+id,{method:'DELETE'}).then(loadClients)}document.getElementById('hideAdminTests').onchange=e=>localStorage.hideAdmin=e.target.checked;hideAdminTests.checked=localStorage.hideAdmin==='true';api('/ping').then(r=>{dot.style.background=r.ok?'var(--ok)':'var(--danger)';statusText.textContent=r.ok?'Conectado':'Offline'});connectAdminSocket();
</script></body></html>"""



class AdminPanel:
    """Admin web panel served at /admin on the existing aiohttp server."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        admin_token: str = "",
        on_tts_changed: Callable[[], Awaitable[None]] | None = None,
        on_api_client_revoked: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.on_tts_changed = on_tts_changed
        self.on_api_client_revoked = on_api_client_revoked
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
        return web.Response(
            text=ADMIN_HTML.replace("__ADMIN_TEST_USER_ID__", str(self.settings.admin_test_user_id)),
            content_type="text/html",
        )

    # ── API: Ping ──────────────────────────────────────────────────

    async def handle_ping(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        return web.json_response({"ok": True, "time": time.time()})

    async def handle_api_clients(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        if request.method == "GET":
            items = await models.list_api_clients(self.db)
            for item in items:
                for key in ("created_at", "revoked_at"):
                    if item.get(key) is not None:
                        item[key] = item[key].isoformat()
            return web.json_response({"items": items})
        if request.method == "DELETE":
            try:
                client_id = int(request.match_info["id"])
            except (KeyError, ValueError):
                return web.json_response({"error": "ID inválido"}, status=400)
            ok = await models.revoke_api_client(self.db, client_id)
            if ok and self.on_api_client_revoked is not None:
                await self.on_api_client_revoked(client_id)
            return web.json_response({"ok": ok}, status=200 if ok else 404)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "JSON inválido"}, status=400)
        name = str(data.get("name") or "").strip()[:120]
        allowed = {"tts:generate", "tts:subscribe", "status:read"}
        scopes = [str(scope) for scope in data.get("scopes", []) if str(scope) in allowed]
        if not name or not scopes:
            return web.json_response({"error": "Nome e ao menos um escopo são obrigatórios."}, status=400)
        raw_key = "gk_" + secrets.token_urlsafe(32)
        key_prefix = raw_key[:12]
        salt = secrets.token_hex(16)
        key_hash = f"sha256${salt}${hashlib.sha256((salt + raw_key).encode()).hexdigest()}"
        item = await models.create_api_client(self.db, name, key_prefix, key_hash, scopes)
        item["created_at"] = item["created_at"].isoformat()
        return web.json_response({"key": raw_key, "client": item}, status=201)

    # ── API: TTS Queue ─────────────────────────────────────────────

    async def get_tts_queue_items(self) -> list[dict[str, object]]:
        rows = await self.db.fetch(
            """
            SELECT t.id, t.texto_original, t.texto_falado, t.audio_url, t.status,
                   t.aprovado, t.source, u.id AS usuario_id, u.nome AS username
            FROM tts_solicitacoes t
            JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.status = 'concluido' AND t.source IN ('admin', 'live')
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
                "usuario_id": r["usuario_id"],
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
                       NULL AS aprovado, t.source, u.nome AS username
                FROM tts_solicitacoes t
                JOIN usuarios u ON u.id = t.usuario_id
                WHERE t.status = 'concluido' AND t.source IN ('admin', 'live')
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

    async def handle_user_personalities(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        search = request.query.get("search", "")
        try:
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            offset = 0
        items, total, counts = await models.list_personality_users(self.db, search, 50, offset)
        for item in items:
            if item.get("ultimo_contato") is not None:
                item["ultimo_contato"] = item["ultimo_contato"].isoformat()
        return web.json_response({"items": items, "total": total, "counts": counts, "offset": offset})

    async def handle_user_personality(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._auth_error()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        personality = data.get("personalidade")
        if personality in ("", None):
            personality = None
        if personality not in (None, "amigo", "neutro", "inimigo", "evitar", "bloqueado"):
            return web.json_response({"error": "Personalidade inválida"}, status=400)
        channel_id = str(data.get("channel_id") or "").strip()
        if not channel_id:
            return web.json_response({"error": "channel_id obrigatório"}, status=400)
        ok = await models.update_user_personality(
            self.db, channel_id, personality, str(data.get("notas") or "").strip() or None
        )
        return web.json_response({"ok": ok}, status=200 if ok else 404)

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
        app.router.add_get("/admin/api/api-clients", self.handle_api_clients)
        app.router.add_post("/admin/api/api-clients", self.handle_api_clients)
        app.router.add_delete("/admin/api/api-clients/{id}", self.handle_api_clients)
        app.router.add_get("/admin/api/tts-queue", self.handle_tts_queue)
        app.router.add_put("/admin/api/tts-queue/{id}", self.handle_tts_approve)
        app.router.add_get("/admin/api/user-personalities", self.handle_user_personalities)
        app.router.add_post("/admin/api/user-personality", self.handle_user_personality)
        app.router.add_get("/admin/api/audio-stats", self.handle_audio_stats)
        app.router.add_post("/admin/api/cleanup", self.handle_cleanup)
        app.router.add_post("/admin/api/terminal", self.handle_terminal)
        logger.info("Admin panel routes registered at /admin")
