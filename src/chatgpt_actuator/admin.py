from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread
from urllib.parse import parse_qs, urlparse

from .runtime import RuntimeManager


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT-Actuator Admin</title>
<style>
:root{font-family:Segoe UI,Arial,sans-serif;color-scheme:light dark}
body{margin:0;background:#111827;color:#e5e7eb}
header{padding:18px 24px;background:#0f172a;border-bottom:1px solid #334155}
h1{margin:0;font-size:21px}.sub{color:#94a3b8;margin-top:5px;font-size:13px}
main{max-width:1180px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:15px}
.card h2{font-size:16px;margin:0 0 12px}
label{display:flex;align-items:center;gap:9px;margin:8px 0;font-size:14px}
input[type=checkbox]{width:17px;height:17px}
input[type=text],textarea{width:100%;box-sizing:border-box;background:#0f172a;color:#e5e7eb;border:1px solid #475569;border-radius:7px;padding:8px}
textarea{min-height:76px;resize:vertical}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
button{border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer;background:#2563eb;color:white}
button.secondary{background:#475569}
button.danger{background:#b91c1c}
.status{padding:10px 12px;border-radius:8px;background:#0f172a;border:1px solid #334155;white-space:pre-wrap}
.audit{font-family:Consolas,monospace;font-size:12px;max-height:330px;overflow:auto;background:#020617;padding:12px;border-radius:8px}
.audit div{padding:4px 0;border-bottom:1px solid #1e293b}
.small{font-size:12px;color:#94a3b8}
.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#064e3b;color:#a7f3d0;font-size:12px}
</style>
</head>
<body>
<header>
<h1>ChatGPT-Actuator Admin</h1>
<div class="sub">Local permission manager · loopback only · <span id="version"></span></div>
</header>
<main>
<div class="status" id="status">Loading...</div>

<div class="actions">
<button onclick="save()">Save & apply now</button>
<button class="secondary" onclick="load()">Refresh</button>
<button class="secondary" onclick="reloadDisk()">Reload config.json</button>
</div>

<div class="grid">
<div class="card"><h2>Filesystem</h2>
<label><input id="fs_enabled" type="checkbox"> Enabled</label>
<label><input id="fs_readonly" type="checkbox"> Read only</label>
<label><input id="fs_create" type="checkbox"> Allow create</label>
<label><input id="fs_edit" type="checkbox"> Allow edit</label>
<label><input id="fs_move" type="checkbox"> Allow move/rename</label>
<label><input id="fs_delete" type="checkbox"> Allow delete</label>
<div class="small">Allowed roots, one per line</div>
<textarea id="fs_roots"></textarea>
</div>

<div class="card"><h2>Process & System</h2>
<label><input id="system_enabled" type="checkbox"> System info enabled</label>
<label><input id="process_enabled" type="checkbox"> Process tools enabled</label>
<label><input id="process_start" type="checkbox"> Allow start</label>
<label><input id="process_stop" type="checkbox"> Allow stop</label>
<label><input id="process_force" type="checkbox"> Allow force kill</label>
</div>

<div class="card"><h2>PowerShell</h2>
<label><input id="ps_enabled" type="checkbox"> Enabled</label>
<label><input id="ps_execute" type="checkbox"> Allow execute</label>
<div class="small">Default working directory</div>
<input id="ps_cwd" type="text">
<div class="small" style="margin-top:8px">Allowed working roots, one per line</div>
<textarea id="ps_roots"></textarea>
</div>

<div class="card"><h2>Screen & Window</h2>
<label><input id="screen_enabled" type="checkbox"> Screenshot enabled</label>
<label><input id="window_enabled" type="checkbox"> Window tools enabled</label>
<label><input id="window_focus" type="checkbox"> Allow focus</label>
<label><input id="window_state" type="checkbox"> Allow state changes</label>
<label><input id="window_close" type="checkbox"> Allow close</label>
</div>

<div class="card"><h2>Mouse</h2>
<label><input id="mouse_enabled" type="checkbox"> Enabled</label>
<label><input id="mouse_move" type="checkbox"> Allow move</label>
<label><input id="mouse_click" type="checkbox"> Allow click</label>
<label><input id="mouse_scroll" type="checkbox"> Allow scroll</label>
</div>

<div class="card"><h2>Keyboard</h2>
<label><input id="keyboard_enabled" type="checkbox"> Enabled</label>
<label><input id="keyboard_write" type="checkbox"> Allow write</label>
<label><input id="keyboard_press" type="checkbox"> Allow press</label>
<label><input id="keyboard_hotkey" type="checkbox"> Allow hotkey</label>
</div>

<div class="card"><h2>Clipboard & Verified Input</h2>
<label><input id="clipboard_enabled" type="checkbox"> Clipboard enabled</label>
<label><input id="clipboard_read" type="checkbox"> Allow read</label>
<label><input id="clipboard_write" type="checkbox"> Allow write</label>
<label><input id="verified_enabled" type="checkbox"> Verified input enabled</label>
<label><input id="verified_verify" type="checkbox"> Verify keyboard writes</label>
<label><input id="verified_repair" type="checkbox"> Repair full-selection mismatch</label>
<label><input id="verified_rollback" type="checkbox"> Roll back partial mismatch</label>
</div>

<div class="card"><h2>UI Automation</h2>
<label><input id="uia_enabled" type="checkbox"> Enabled</label>
<label><input id="uia_focus" type="checkbox"> Allow focus</label>
<label><input id="uia_invoke" type="checkbox"> Allow invoke</label>
<label><input id="uia_value" type="checkbox"> Allow set value</label>
<label><input id="uia_toggle" type="checkbox"> Allow toggle</label>
<label><input id="uia_select" type="checkbox"> Allow select</label>
<label><input id="uia_expand" type="checkbox"> Allow expand/collapse</label>
<label><input id="uia_scroll" type="checkbox"> Allow semantic scroll</label>
<label><input id="uia_scroll_view" type="checkbox"> Allow scroll into view</label>
<label><input id="uia_range" type="checkbox"> Allow range value</label>
<label><input id="uia_text_select" type="checkbox"> Allow text selection</label>
<label><input id="uia_window_action" type="checkbox"> Allow WindowPattern actions</label>
<div class="small">UIA actions are background-first; physical mouse/keyboard input is not used.</div>
</div>
</div>

<div class="actions">
<button class="secondary" onclick="loadAudit()">Refresh audit</button>
</div>
<div class="audit" id="audit"></div>
</main>
<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const checked = id => $(id).checked;
const lines = id => $(id).value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
function setStatus(msg, ok=true){ $('status').textContent=msg; $('status').style.borderColor=ok?'#065f46':'#991b1b'; }
async function api(path, opts={}){
  opts.headers = Object.assign({'Accept':'application/json'}, opts.headers||{});
  opts.headers['X-Actuator-Admin-Token']=TOKEN;
  const r=await fetch(path,opts);
  const body=await r.json().catch(()=>({error:'Invalid JSON response'}));
  if(r.status===403 && body.error==='Invalid admin session token.'){
    setTimeout(()=>location.reload(),50);
    throw new Error('Admin session expired. Reloading with a fresh local session...');
  }
  if(!r.ok) throw new Error(body.error||('HTTP '+r.status));
  return body;
}
function fill(s){
  const c=s.capabilities;
  $('version').textContent=s.version+' · generation '+s.generation;
  $('fs_enabled').checked=c.filesystem.enabled;$('fs_readonly').checked=c.filesystem.read_only;
  $('fs_create').checked=c.filesystem.allow_create;$('fs_edit').checked=c.filesystem.allow_edit;
  $('fs_move').checked=c.filesystem.allow_move;$('fs_delete').checked=c.filesystem.allow_delete;
  $('fs_roots').value=c.filesystem.allowed_roots.join('\n');
  $('system_enabled').checked=c.system.enabled;
  $('process_enabled').checked=c.process.enabled;$('process_start').checked=c.process.allow_start;
  $('process_stop').checked=c.process.allow_stop;$('process_force').checked=c.process.allow_force_kill;
  $('ps_enabled').checked=c.powershell.enabled;$('ps_execute').checked=c.powershell.allow_execute;
  $('ps_cwd').value=c.powershell.default_cwd;$('ps_roots').value=c.powershell.allowed_working_roots.join('\n');
  $('screen_enabled').checked=c.screen.enabled;$('window_enabled').checked=c.window.enabled;
  $('window_focus').checked=c.window.allow_focus;$('window_state').checked=c.window.allow_state_change;$('window_close').checked=c.window.allow_close;
  $('mouse_enabled').checked=c.mouse.enabled;$('mouse_move').checked=c.mouse.allow_move;$('mouse_click').checked=c.mouse.allow_click;$('mouse_scroll').checked=c.mouse.allow_scroll;
  $('keyboard_enabled').checked=c.keyboard.enabled;$('keyboard_write').checked=c.keyboard.allow_write;$('keyboard_press').checked=c.keyboard.allow_press;$('keyboard_hotkey').checked=c.keyboard.allow_hotkey;
  $('clipboard_enabled').checked=c.clipboard.enabled;$('clipboard_read').checked=c.clipboard.allow_read;$('clipboard_write').checked=c.clipboard.allow_write;
  $('verified_enabled').checked=c.verified_input.enabled;$('verified_verify').checked=c.verified_input.verify_keyboard_write;
  $('verified_repair').checked=c.verified_input.repair_full_selection_failure;$('verified_rollback').checked=c.verified_input.rollback_partial_failure;
  $('uia_enabled').checked=c.ui_automation.enabled;$('uia_focus').checked=c.ui_automation.allow_focus;$('uia_invoke').checked=c.ui_automation.allow_invoke;
  $('uia_value').checked=c.ui_automation.allow_set_value;$('uia_toggle').checked=c.ui_automation.allow_toggle;$('uia_select').checked=c.ui_automation.allow_select;$('uia_expand').checked=c.ui_automation.allow_expand_collapse;
  $('uia_scroll').checked=c.ui_automation.allow_scroll;$('uia_scroll_view').checked=c.ui_automation.allow_scroll_into_view;$('uia_range').checked=c.ui_automation.allow_range_value;$('uia_text_select').checked=c.ui_automation.allow_text_selection;$('uia_window_action').checked=c.ui_automation.allow_window_action;
}
function applyPendingPatch(p){
  if(!p) return;
  const set=(id,v)=>{if($(id)) $(id).checked=!!v;};
  const c=p;
  if(c.filesystem){set('fs_enabled',c.filesystem.enabled);set('fs_readonly',c.filesystem.read_only);set('fs_create',c.filesystem.allow_create);set('fs_edit',c.filesystem.allow_edit);set('fs_move',c.filesystem.allow_move);set('fs_delete',c.filesystem.allow_delete);if(c.filesystem.allowed_roots)$('fs_roots').value=c.filesystem.allowed_roots.join('\n');}
  if(c.system)set('system_enabled',c.system.enabled);
  if(c.process){set('process_enabled',c.process.enabled);set('process_start',c.process.allow_start);set('process_stop',c.process.allow_stop);set('process_force',c.process.allow_force_kill);}
  if(c.powershell){set('ps_enabled',c.powershell.enabled);set('ps_execute',c.powershell.allow_execute);if(c.powershell.default_cwd!==undefined)$('ps_cwd').value=c.powershell.default_cwd;if(c.powershell.allowed_working_roots)$('ps_roots').value=c.powershell.allowed_working_roots.join('\n');}
  if(c.screen)set('screen_enabled',c.screen.enabled);
  if(c.window){set('window_enabled',c.window.enabled);set('window_focus',c.window.allow_focus);set('window_state',c.window.allow_state_change);set('window_close',c.window.allow_close);}
  if(c.mouse){set('mouse_enabled',c.mouse.enabled);set('mouse_move',c.mouse.allow_move);set('mouse_click',c.mouse.allow_click);set('mouse_scroll',c.mouse.allow_scroll);}
  if(c.keyboard){set('keyboard_enabled',c.keyboard.enabled);set('keyboard_write',c.keyboard.allow_write);set('keyboard_press',c.keyboard.allow_press);set('keyboard_hotkey',c.keyboard.allow_hotkey);}
  if(c.clipboard){set('clipboard_enabled',c.clipboard.enabled);set('clipboard_read',c.clipboard.allow_read);set('clipboard_write',c.clipboard.allow_write);}
  if(c.verified_input){set('verified_enabled',c.verified_input.enabled);set('verified_verify',c.verified_input.verify_keyboard_write);set('verified_repair',c.verified_input.repair_full_selection_failure);set('verified_rollback',c.verified_input.rollback_partial_failure);}
  if(c.ui_automation){set('uia_enabled',c.ui_automation.enabled);set('uia_focus',c.ui_automation.allow_focus);set('uia_invoke',c.ui_automation.allow_invoke);set('uia_value',c.ui_automation.allow_set_value);set('uia_toggle',c.ui_automation.allow_toggle);set('uia_select',c.ui_automation.allow_select);set('uia_expand',c.ui_automation.allow_expand_collapse);set('uia_scroll',c.ui_automation.allow_scroll);set('uia_scroll_view',c.ui_automation.allow_scroll_into_view);set('uia_range',c.ui_automation.allow_range_value);set('uia_text_select',c.ui_automation.allow_text_selection);set('uia_window_action',c.ui_automation.allow_window_action);}
}
async function load(){
  try{
    const s=await api('/api/status');
    fill(s);
    const pending=sessionStorage.getItem('actuator_pending_patch');
    if(pending){
      try{applyPendingPatch(JSON.parse(pending));}catch(_e){}
      sessionStorage.removeItem('actuator_pending_patch');
      setStatus('Admin session refreshed. Your unsaved choices were restored; review them and press Save & apply now.');
    }else{
      setStatus('Ready · runtime generation '+s.generation);
    }
    await loadAudit();
  }catch(e){setStatus(e.message,false);}
}
function patch(){
 return {
  filesystem:{enabled:checked('fs_enabled'),read_only:checked('fs_readonly'),allow_create:checked('fs_create'),allow_edit:checked('fs_edit'),allow_move:checked('fs_move'),allow_delete:checked('fs_delete'),allowed_roots:lines('fs_roots')},
  system:{enabled:checked('system_enabled')},
  process:{enabled:checked('process_enabled'),allow_start:checked('process_start'),allow_stop:checked('process_stop'),allow_force_kill:checked('process_force')},
  powershell:{enabled:checked('ps_enabled'),allow_execute:checked('ps_execute'),default_cwd:$('ps_cwd').value.trim(),allowed_working_roots:lines('ps_roots')},
  screen:{enabled:checked('screen_enabled')},
  window:{enabled:checked('window_enabled'),allow_focus:checked('window_focus'),allow_state_change:checked('window_state'),allow_close:checked('window_close')},
  mouse:{enabled:checked('mouse_enabled'),allow_move:checked('mouse_move'),allow_click:checked('mouse_click'),allow_scroll:checked('mouse_scroll')},
  keyboard:{enabled:checked('keyboard_enabled'),allow_write:checked('keyboard_write'),allow_press:checked('keyboard_press'),allow_hotkey:checked('keyboard_hotkey')},
  clipboard:{enabled:checked('clipboard_enabled'),allow_read:checked('clipboard_read'),allow_write:checked('clipboard_write')},
  verified_input:{enabled:checked('verified_enabled'),verify_keyboard_write:checked('verified_verify'),repair_full_selection_failure:checked('verified_repair'),rollback_partial_failure:checked('verified_rollback')},
  ui_automation:{enabled:checked('uia_enabled'),allow_focus:checked('uia_focus'),allow_invoke:checked('uia_invoke'),allow_set_value:checked('uia_value'),allow_toggle:checked('uia_toggle'),allow_select:checked('uia_select'),allow_expand_collapse:checked('uia_expand'),allow_scroll:checked('uia_scroll'),allow_scroll_into_view:checked('uia_scroll_view'),allow_range_value:checked('uia_range'),allow_text_selection:checked('uia_text_select'),allow_window_action:checked('uia_window_action')}
 };
}
async function save(){
 const requested=patch();
 sessionStorage.setItem('actuator_pending_patch',JSON.stringify(requested));
 try{
  const s=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(requested)});
  sessionStorage.removeItem('actuator_pending_patch');
  fill(s);setStatus('Saved and applied immediately · generation '+s.generation);await loadAudit();
 }catch(e){
  if(!String(e.message||'').includes('Admin session expired')){
    sessionStorage.removeItem('actuator_pending_patch');
  }
  setStatus('Not applied: '+e.message,false);
 }
}
async function reloadDisk(){
 try{const s=await api('/api/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});fill(s);setStatus('Reloaded config.json · generation '+s.generation);await loadAudit();}
 catch(e){setStatus('Reload failed: '+e.message,false);}
}
async function loadAudit(){
 try{
  const x=await api('/api/audit?limit=200');
  $('audit').innerHTML='';
  for(const e of x.events.slice().reverse()){
    const d=document.createElement('div');d.textContent=JSON.stringify(e);$('audit').appendChild(d);
  }
 }catch(e){$('audit').textContent=e.message;}
}
load();
</script>
</body>
</html>
"""


class AdminServer:
    def __init__(self, runtime: RuntimeManager) -> None:
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        cfg = self.runtime.config.admin
        host = "127.0.0.1" if cfg.host == "localhost" else cfg.host
        return f"http://{host}:{cfg.port}/"

    def start(self) -> None:
        cfg = self.runtime.config.admin
        if not cfg.enabled:
            return
        if cfg.host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Admin server refuses non-loopback bind.")

        runtime = self.runtime
        token = self.token
        html = _HTML.replace("__TOKEN__", token)

        class Handler(BaseHTTPRequestHandler):
            server_version = "ChatGPT-Actuator-Admin/1.0"

            def log_message(self, format: str, *args) -> None:
                return

            def _json(self, status: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self) -> bool:
                host_header = self.headers.get("Host", "")
                hostname = host_header.rsplit(":", 1)[0].strip().casefold()
                return hostname in {"127.0.0.1", "localhost"}

            def _token_ok(self) -> bool:
                provided = self.headers.get("X-Actuator-Admin-Token", "")
                return secrets.compare_digest(provided, token)

            def _read_json(self) -> dict:
                raw_len = self.headers.get("Content-Length", "0")
                length = int(raw_len)
                if length < 0 or length > 2_000_000:
                    raise ValueError("Request body is too large.")
                data = self.rfile.read(length)
                value = json.loads(data.decode("utf-8") or "{}")
                if not isinstance(value, dict):
                    raise TypeError("JSON body must be an object.")
                return value

            def do_GET(self) -> None:
                if not self._host_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header."})
                    return
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    data = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
                    )
                    self.end_headers()
                    self.wfile.write(data)
                    return

                if parsed.path == "/api/status":
                    if not self._token_ok():
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid admin session token."})
                        return
                    self._json(HTTPStatus.OK, runtime.status())
                    return

                if parsed.path == "/api/audit":
                    if not self._token_ok():
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid admin session token."})
                        return
                    qs = parse_qs(parsed.query)
                    try:
                        limit = int(qs.get("limit", ["200"])[0])
                        events = runtime.audit_tail(limit)
                        self._json(HTTPStatus.OK, {"events": events})
                    except Exception as exc:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                if parsed.path == "/healthz":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "version": runtime.config.version,
                            "generation": runtime.generation,
                        },
                    )
                    return

                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_POST(self) -> None:
                if not self._host_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header."})
                    return
                if not self._token_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid admin session token."})
                    return

                try:
                    if self.path == "/api/config":
                        if not runtime.config.admin.allow_config_write:
                            raise PermissionError("Admin configuration writes are disabled.")
                        patch = self._read_json()
                        self._json(HTTPStatus.OK, runtime.apply_patch(patch))
                        return

                    if self.path == "/api/reload":
                        self._read_json()
                        self._json(HTTPStatus.OK, runtime.reload_from_disk())
                        return

                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                except Exception as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        self._server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name="ChatGPT-Actuator-Admin",
            daemon=True,
        )
        self._thread.start()
        self.runtime.audit.record(
            "admin_server_started",
            host=cfg.host,
            port=cfg.port,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None
