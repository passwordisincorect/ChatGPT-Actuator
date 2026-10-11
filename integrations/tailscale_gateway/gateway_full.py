"""Separate Full Access MCP resource with scoped OAuth and optional call approval."""
import hashlib
import html
import json
import re
import secrets
import time

import httpx
from gateway_config import load_profile
PROFILE = load_profile()
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SCOPES = {"mcp:read", "mcp:write", "mcp:execute"}
# Full Access OAuth always requires local owner consent.
PER_CALL_APPROVAL_REQUIRED = PROFILE["per_call_approval"]
READ = {"routing_policy_info", "routing_decide", "system_info",
        "system_resource_snapshot", "powershell_info", "eda_info",
        "orchestrator_info", "project_list", "task_list", "task_get",
        "chat_list", "chat_status", "chat_job_list", "chat_job_status",
        "task_review_status"}
WRITE = {"write", "replace", "move", "delete", "create", "update", "attach",
         "set", "click", "press", "hotkey", "toggle", "select", "close",
         "focus", "invoke", "scroll", "navigate", "open", "apply", "materialize"}
# These backends currently run with the owner's Windows identity. An approval
# cannot sandbox arbitrary commands, code-bearing messages, or process launch.
RESTRICTED = {"actuator__powershell_run", "actuator__process_start",
              "actuator__process_stop", "actuator__batch_execute",
              "actuator__dom_open_tab", "actuator__dom_navigate",
              "actuator__dom_close_tab", "actuator__dom_click",
              "actuator__dom_set_value", "actuator__dom_select",
              "actuator__dom_toggle", "actuator__mouse_move",
              "actuator__mouse_click", "actuator__mouse_scroll",
              "actuator__keyboard_write", "actuator__keyboard_press",
              "actuator__keyboard_hotkey", "actuator__text_replace_all",
              "orchestrator__chat_send", "orchestrator__chat_submit",
              "orchestrator__chat_retry", "orchestrator__task_auto_start",
              "orchestrator__task_auto_advance", "orchestrator__task_recover"}
PENDING = {}
EXTRA_READ = {
    "actuator__filesystem_list_roots", "actuator__filesystem_list",
    "actuator__filesystem_read_text", "actuator__filesystem_stat",
    "actuator__filesystem_search", "actuator__screen_info",
    "actuator__window_list", "actuator__window_active",
    "actuator__window_details", "actuator__mouse_position",
    "actuator__clipboard_read_text", "actuator__eda_project_inspect",
    "actuator__eda_sch_inspect", "actuator__eda_sch_worker_status",
    "orchestrator__project_get", "orchestrator__chat_read",
}
EXPOSED = ({"actuator__"+name for name in READ} |
           {"orchestrator__"+name for name in READ} | EXTRA_READ)

def scope_for(name):
    original = name.split("__", 1)[-1]
    if original in READ: return "mcp:read"
    return "mcp:write" if set(original.split("_")) & WRITE else "mcp:execute"

def rpc_error(rpc, code, message, status=200):
    return JSONResponse({"jsonrpc":"2.0", "id":rpc.get("id"),
        "error":{"code":code,"message":message}}, status_code=status,
        headers={"Cache-Control":"no-store"})

def pending_result(rpc, key, g):
    return JSONResponse({"jsonrpc":"2.0", "id":rpc.get("id"),
        "result":{"content":[{"type":"text", "text":
        f"Approve request {key} at http://127.0.0.1:{g.ADMIN_PORT}/full-approvals on the Windows PC, then retry the identical tool call within 120 seconds."}],
        "isError":True}}, headers={"Cache-Control":"no-store"})

def cleanup():
    now = time.monotonic()
    for key, action in list(PENDING.items()):
        if action["expires"] < now or action["status"] in ("used", "denied"):
            PENDING.pop(key, None)

def token_is_live(g, tokenhash, client_id):
    with g.db() as c:
        return c.execute("SELECT 1 FROM tokens t JOIN clients c ON c.client_id=t.client_id "
            "WHERE t.tokenhash=? AND t.kind='access' AND t.client_id=? "
            "AND t.resource=? AND t.expires>?",
            (tokenhash,client_id,g.FULL_RESOURCE,g.stamp())).fetchone() is not None

def proxy_headers(request, method, upstream_name=None):
    if any(k.startswith("mcp-param-") for k in request.headers):
        raise ValueError("Unvalidated MCP header parameter")
    headers={"Content-Type":"application/json", "Accept":"application/json, text/event-stream"}
    if request.headers.get("mcp-protocol-version"):
        headers["Mcp-Protocol-Version"]=request.headers["mcp-protocol-version"]
    if request.headers.get("mcp-method"): headers["Mcp-Method"]=method
    if upstream_name and request.headers.get("mcp-name"):
        headers["Mcp-Name"]=upstream_name
    return headers

async def backend_post(url, payload, headers):
    async with httpx.AsyncClient(timeout=25,trust_env=False) as client:
        response=await client.post(url,json=payload,headers=headers)
    if len(response.content)>4194304: raise ValueError("Oversized backend response")
    return response

async def tool_map(g, rpc, headers):
    found={}
    for prefix,url in (("actuator",g.BACKEND),):
        cursor=None
        local={}
        try:
            for _ in range(10):
                original_params=rpc.get("params")
                params={"_meta":original_params.get("_meta",{}) if isinstance(original_params,dict) else {}}
                if cursor: params["cursor"]=cursor
                else: params.pop("cursor",None)
                payload={"jsonrpc":"2.0","id":rpc.get("id"),"method":"tools/list","params":params}
                response=await backend_post(url,payload,{**headers,"Mcp-Method":"tools/list"})
                if response.status_code!=200: raise ValueError("Backend unavailable")
                data=response.json()
                if not isinstance(data,dict): raise ValueError("Invalid backend response")
                result=data.get("result")
                if not isinstance(result,dict): raise ValueError("Invalid backend result")
                tools=result.get("tools")
                if not isinstance(tools,list): raise ValueError("Invalid tool list")
                for t in tools:
                    if not isinstance(t,dict) or not isinstance(t.get("name"),str) or not isinstance(t.get("inputSchema"),dict):
                        continue
                    name=prefix+"__"+t["name"]
                    if prefix != "actuator" and name not in EXPOSED: continue
                    item=dict(t)
                    item["name"]=name
                    item["title"]=prefix.title()+": "+str(t.get("title") or t["name"])
                    if name in RESTRICTED and PER_CALL_APPROVAL_REQUIRED:
                        item["description"]=str(t.get("description") or "")+" Requires one-time local owner approval; runs with the backend Windows user identity."
                    item["securitySchemes"]=[{"type":"oauth2","scopes":[scope_for(name)]}]
                    item["annotations"]={**(t.get("annotations") or {}),
                                         "readOnlyHint":name.split("__",1)[-1] in READ}
                    local[name]=(item,url,t["name"])
                next_cursor=result.get("nextCursor")
                if not next_cursor: break
                if next_cursor==cursor: raise ValueError("Repeated cursor")
                cursor=next_cursor
            else: raise ValueError("Excess pagination")
            found.update(local)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("Actuator backend unavailable") from exc
    if not found: raise ValueError("Actuator returned no tools")
    return found

async def full_proxy(request,g):
    if (g.ROOT/"FULL_ACCESS_DISABLED").exists(): return Response(status_code=503)
    values=request.headers.getlist("authorization")
    if len(values)!=1 or not values[0].startswith("Bearer "): return g.full_challenge()
    supplied=values[0][7:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{48,120}",supplied): return g.full_challenge()
    tokenhash=g.digest(supplied)
    with g.db() as c:
        tok=c.execute("SELECT * FROM tokens WHERE tokenhash=? AND kind='access'",(tokenhash,)).fetchone()
        client=c.execute("SELECT 1 FROM clients WHERE client_id=?",(tok["client_id"],)).fetchone() if tok else None
    if (not tok or not client or tok["resource"]!=g.FULL_RESOURCE
            or tok["expires"]<=g.stamp() or not SCOPES.issubset(set(tok["scope"].split()))):
        return g.full_challenge()
    if request.headers.get("origin"): return Response(status_code=403)
    if request.method!="POST": return Response(status_code=405,headers={"Allow":"POST"})
    if "application/json" not in request.headers.get("content-type","").lower(): return Response(status_code=415)
    raw=await request.body()
    if len(raw)>1048576: return Response(status_code=413)
    try: rpc=json.loads(raw)
    except (ValueError,UnicodeError): return Response(status_code=400)
    if not isinstance(rpc,dict) or rpc.get("method") not in g.ALLOWED_METHODS: return Response(status_code=403)
    method=rpc["method"]
    if request.headers.get("mcp-protocol-version")=="2026-07-28" and request.headers.get("mcp-method")!=method:
        return Response(status_code=400)
    try: headers=proxy_headers(request,method)
    except ValueError: return Response(status_code=400)
    if method=="tools/list":
        try: mapping=await tool_map(g,rpc,headers)
        except ValueError: return rpc_error(rpc,-32603,"Actuator backend unavailable",502)
        return JSONResponse({"jsonrpc":"2.0","id":rpc.get("id"),
            "result":{"tools":[entry[0] for entry in mapping.values()]}},headers={"Cache-Control":"no-store"})
    if method=="tools/call":
        params=rpc.get("params")
        name=params.get("name") if isinstance(params,dict) else None
        args=params.get("arguments",{}) if isinstance(params,dict) else None
        if not isinstance(name,str) or not isinstance(args,dict): return rpc_error(rpc,-32602,"Invalid arguments")
        if request.headers.get("mcp-protocol-version")=="2026-07-28" and request.headers.get("mcp-name")!=name:
            return Response(status_code=400)
        try: mapping=await tool_map(g,rpc,headers)
        except ValueError: return rpc_error(rpc,-32603,"Actuator backend unavailable",502)
        if name not in mapping: return rpc_error(rpc,-32602,"Unknown or offline tool")
        if not token_is_live(g,tokenhash,tok["client_id"]): return g.full_challenge()
        if scope_for(name) not in tok["scope"].split(): return Response(status_code=403)
        if name.split("__",1)[-1] not in READ:
            if len(json.dumps(args,ensure_ascii=False).encode("utf-8"))>16384: return Response(status_code=413)
        if PER_CALL_APPROVAL_REQUIRED and name.split("__",1)[-1] not in READ:
            cleanup()
            fp=hashlib.sha256(json.dumps([tokenhash,tok["client_id"],name,args],
                   sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
            existing=next(((k,a) for k,a in PENDING.items() if a["fingerprint"]==fp),None)
            if existing is None:
                if len(PENDING)>=100: return Response(status_code=429)
                key=secrets.token_urlsafe(24)
                PENDING[key]={"fingerprint":fp,"tokenhash":tokenhash,"client_id":tok["client_id"],
                    "name":name,"args":args,"expires":time.monotonic()+120,"status":"pending"}
                return pending_result(rpc,key,g)
            key,action=existing
            if action["status"]!="approved": return pending_result(rpc,key,g)
            action["status"]="used"  # no await between check and consumption
            PENDING.pop(key,None)
        _,url,upstream_name=mapping[name]
        outgoing=dict(rpc)
        outgoing["params"]={**params,"name":upstream_name}
        try:
            response=await backend_post(url,outgoing,proxy_headers(request,method,upstream_name))
            return Response(response.content,status_code=response.status_code,
                media_type=response.headers.get("content-type","application/json").split(";",1)[0],
                headers={"Cache-Control":"no-store"})
        except (httpx.HTTPError,ValueError): return rpc_error(rpc,-32603,"Backend unavailable",502)
    for url in (g.BACKEND,):
        try:
            response=await backend_post(url,rpc,headers)
            if method=="notifications/initialized" and response.status_code in (200,202,204):
                return Response(status_code=202,headers={"Cache-Control":"no-store"})
            if response.status_code!=200: continue
            data=response.json()
            if method=="server/discover": data["result"]["capabilities"]={"tools":{"listChanged":False}}
            return JSONResponse(data,headers={"Cache-Control":"no-store"})
        except (httpx.HTTPError,ValueError,TypeError,KeyError): continue
    return rpc_error(rpc,-32603,"Backend unavailable",502)

async def approval_page(request,g):
    if not g.admin_headers_ok(request): return Response(status_code=403)
    cleanup()
    rows=[]
    for key,a in PENDING.items():
        if a["status"]!="pending": continue
        rows.append("<li><b>"+html.escape(a["name"])+"</b> client ..."+
            html.escape(a["client_id"][-8:])+"<pre>"+
            html.escape(json.dumps(a["args"],ensure_ascii=False,indent=2))+"</pre>"+
            '<form method="post" action="/full-decision">'+
            '<input type="hidden" name="id" value="'+html.escape(key,quote=True)+'">'+
            '<input type="hidden" name="csrf" value="'+html.escape(g.CSRF,quote=True)+'">'+
            '<button name="choice" value="approve">Approve once</button> '+
            '<button name="choice" value="deny">Deny</button></form></li>')
    return HTMLResponse("<!doctype html><html><head><meta name='referrer' content='no-referrer'></head>"
        "<body><h2>Full Access approvals</h2><ul>"+("".join(rows) or "<li>No pending actions</li>")+"</ul></body></html>",
        headers={"Cache-Control":"no-store","X-Frame-Options":"DENY",
                 "Content-Security-Policy":"default-src 'none'; form-action 'self'",
                 "Referrer-Policy":"no-referrer"})

async def decision(request,g):
    if not g.admin_headers_ok(request) or request.headers.get("origin")!=f"http://127.0.0.1:{g.ADMIN_PORT}":
        return Response(status_code=403)
    fields=await g.parse_form(request,4096)
    if not g.hmac.compare_digest(fields.get("csrf",""),g.CSRF): return Response(status_code=403)
    cleanup()
    action=PENDING.get(fields.get("id",""))
    if not action or action["status"]!="pending": return Response(status_code=410)
    if fields.get("choice") not in ("approve","deny"): return Response(status_code=400)
    with g.db() as c:
        valid=c.execute("SELECT 1 FROM tokens WHERE tokenhash=? AND kind='access' AND expires>? AND client_id=? AND resource=?",
            (action["tokenhash"],g.stamp(),action["client_id"],g.FULL_RESOURCE)).fetchone()
    if not valid: return Response(status_code=410)
    action["status"]="approved" if fields["choice"]=="approve" else "denied"
    return RedirectResponse("/full-approvals",status_code=303)
