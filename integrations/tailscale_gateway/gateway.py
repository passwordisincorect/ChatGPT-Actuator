"""Owner-approved OAuth 2.1 gateway for Windows MCP tools.
Never expose the underlying Actuator HTTP port directly to the Internet.
The approval console listens on loopback only, separately from Funnel.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from gateway_config import load_profile
PROFILE = load_profile()
from gateway_guard import RequestGuard
import gateway_full as full
import owner_ui

ROOT = PROFILE["state_dir"]
DB = ROOT / "gateway.sqlite3"
ISSUER = PROFILE["issuer"]
RESOURCE = ISSUER + "/mcp"
ORCH_RESOURCE = ISSUER + "/orch/mcp"
FULL_RESOURCE = ISSUER + "/full/mcp"
BACKEND = PROFILE["actuator_backend"]
ORCH_BACKEND = PROFILE["orchestrator_backend"]
# Public tool name -> original Orchestrator method. No execution/modification tools
# are internet-accessible in this stage.
ORCH_READONLY = {
    "orch_info": "orchestrator_info",
    "orch_project_list": "project_list",
    "orch_task_list": "task_list",
    "orch_task_get": "task_get",
    "orch_chat_list": "chat_list",
    "orch_chat_status": "chat_status",
    "orch_chat_job_list": "chat_job_list",
    "orch_chat_job_status": "chat_job_status",
    "orch_task_review_status": "task_review_status",
}
PUBLIC_PORT = PROFILE["public_port"]
ADMIN_PORT = PROFILE["owner_port"]
SCOPE = "mcp:read"
WRITE_SCOPE = "mcp:write"
EXECUTE_SCOPE = "mcp:execute"
OFFLINE_SCOPE = "offline_access"
ACTUATOR_READONLY = frozenset({
    "routing_policy_info",
    "routing_decide",
    "system_info",
    "system_resource_snapshot",
    "powershell_info",
    "eda_info",
})
# Explicit, reviewed public names. The original /mcp route remains read-only.
ORCH_MORE_READONLY = {
    "orch_project_get": "project_get",
    "orch_chat_read": "chat_read",
}
ORCH_WRITE = {
    "orch_project_create": "project_create",
    "orch_project_update": "project_update",
    "orch_project_attach_task": "project_attach_task",
    "orch_task_create": "task_create",
    "orch_task_plan": "task_plan",
    "orch_chat_create": "chat_create",
}
ORCH_EXECUTE = {
    "orch_task_auto_start": "task_auto_start",
    "orch_task_auto_advance": "task_auto_advance",
    "orch_task_recover": "task_recover",
    "orch_task_cancel": "task_cancel",
    "orch_task_collect": "task_collect",
    "orch_task_review_submit": "task_review_submit",
    "orch_task_finalize": "task_finalize",
    "orch_chat_send": "chat_send",
    "orch_chat_submit": "chat_submit",
    "orch_chat_cancel": "chat_cancel",
    "orch_chat_retry": "chat_retry",
    "orch_chat_close": "chat_close",
}
ORCH_ALL = {**ORCH_READONLY, **ORCH_MORE_READONLY, **ORCH_WRITE, **ORCH_EXECUTE}
ORCH_SCOPE_MAP = {
    **{k: SCOPE for k in (*ORCH_READONLY, *ORCH_MORE_READONLY)},
    **{k: WRITE_SCOPE for k in ORCH_WRITE},
    **{k: EXECUTE_SCOPE for k in ORCH_EXECUTE},
}
assert len(ORCH_ALL)==29 and len(ORCH_SCOPE_MAP)==29
ALLOWED_TOOLS = frozenset({*ACTUATOR_READONLY, *ORCH_READONLY.keys()})
ALLOWED_METHODS = frozenset({"server/discover", "initialize", "notifications/initialized", "ping", "tools/list", "tools/call"})
APPROVALS: dict[str, dict] = {}
CSRF = secrets.token_urlsafe(32)

def stamp(): return int(time.time())
def digest(s: str) -> str: return hashlib.sha256(s.encode("utf-8")).hexdigest()
def pkce_s256(s: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(s.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
def response_error(error: str, status: int = 400):
    return JSONResponse({"error": error}, status_code=status, headers={"Cache-Control": "no-store"})
@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def initialize_db():
    ROOT.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS clients(
           client_id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect TEXT NOT NULL, created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codes(
           codehash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect TEXT NOT NULL,
           challenge TEXT NOT NULL, resource TEXT NOT NULL, expires INTEGER NOT NULL, scope TEXT NOT NULL DEFAULT 'mcp:read'
        );
        CREATE TABLE IF NOT EXISTS tokens(
           tokenhash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
           resource TEXT NOT NULL, scope TEXT NOT NULL, expires INTEGER NOT NULL
        );
        """)
        # Upgrade an existing database without losing registered clients.
        columns = {r["name"] for r in c.execute("PRAGMA table_info(codes)").fetchall()}
        if "scope" not in columns:
            c.execute("ALTER TABLE codes ADD COLUMN scope TEXT NOT NULL DEFAULT 'mcp:read'")

def oauth_meta(request: Request):
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/oauth/authorize",
        "token_endpoint": ISSUER + "/oauth/token",
        "registration_endpoint": ISSUER + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE, "mcp:write", "mcp:execute", OFFLINE_SCOPE],
        "authorization_response_iss_parameter_supported": True
    }, headers={"Cache-Control": "public, max-age=600"})

def resource_meta(request: Request):
    return JSONResponse({
        "resource": (FULL_RESOURCE if request.url.path.endswith("/full/mcp")
                     else ORCH_RESOURCE if request.url.path.endswith("/orch/mcp")
                     else RESOURCE if request.url.path.endswith("/mcp") else ISSUER),
        "authorization_servers": [ISSUER],
        "scopes_supported": ([SCOPE, "mcp:write", "mcp:execute", OFFLINE_SCOPE]
                             if request.url.path.endswith(("/full/mcp", "/orch/mcp"))
                             else [SCOPE, OFFLINE_SCOPE])
    }, headers={"Cache-Control": "public, max-age=600"})

def valid_redirect(uri: str) -> bool:
    try:
        u = urlparse(uri)
        return (u.scheme == "https" and u.netloc == "chatgpt.com"
            and not u.fragment and (u.path == "/connector_platform_oauth_redirect"
                or (u.path.startswith("/connector/oauth/") and len(u.path) > len("/connector/oauth/"))))
    except (TypeError, ValueError):
        return False

async def register(request: Request):
    # Dynamic Client Registration: only ChatGPT's documented callback addresses.
    if int(request.headers.get("content-length", "0") or "0") > 8192:
        return response_error("invalid_client_metadata", 413)
    raw = await request.body()
    if len(raw) > 8192: return response_error("invalid_client_metadata", 413)
    try: data = json.loads(raw)
    except (ValueError, UnicodeError): return response_error("invalid_client_metadata")
    if not isinstance(data, dict): return response_error("invalid_client_metadata")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or len(uris) != 1 or not valid_redirect(uris[0]):
        return response_error("invalid_redirect_uri")
    authmethod = data.get("token_endpoint_auth_method", "none")
    if authmethod != "none": return response_error("invalid_client_metadata")
    # Reflect the client's requested, supported scopes in the DCR response.
    # Registration alone never authorizes them; /oauth/authorize still checks
    # the resource and requires explicit owner approval.
    requested_scope = data.get("scope")
    if requested_scope is None:
        registered_scope = SCOPE + " " + OFFLINE_SCOPE
    else:
        if not isinstance(requested_scope, str):
            return response_error("invalid_client_metadata")
        items = requested_scope.split()
        scopes = set(items)
        supported = {SCOPE, WRITE_SCOPE, EXECUTE_SCOPE, OFFLINE_SCOPE}
        if (not scopes.intersection({SCOPE, WRITE_SCOPE, EXECUTE_SCOPE})
                or len(scopes) != len(items) or not scopes.issubset(supported)):
            return response_error("invalid_client_metadata")
        registered_scope = " ".join(s for s in (SCOPE, WRITE_SCOPE, EXECUTE_SCOPE, OFFLINE_SCOPE) if s in scopes)
    client_id = "mcp_" + secrets.token_urlsafe(24)
    name = str(data.get("client_name") or "ChatGPT")[:80]
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= 500:
            return response_error("temporarily_unavailable", 503)
        c.execute("INSERT INTO clients VALUES (?,?,?,?)", (client_id, name, uris[0], stamp()))
    return JSONResponse({"client_id": client_id, "client_name": name,
        "redirect_uris": uris, "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": registered_scope
    }, status_code=201, headers={"Cache-Control": "no-store"})

def url_with(uri: str, fields: dict) -> str:
    separator = "&" if "?" in uri else "?"
    return uri + separator + urlencode(fields)

async def authorize(request: Request):
    q = request.query_params
    if any(len(q.getlist(k)) != 1 for k in q):
        return response_error("invalid_request")
    client_id = q.get("client_id", "")
    redirect = q.get("redirect_uri", "")
    challenge = q.get("code_challenge", "")
    resource = q.get("resource", "")
    if resource == FULL_RESOURCE and (ROOT / "FULL_ACCESS_DISABLED").exists(): return response_error("temporarily_unavailable", 503)
    requested_scopes = q.get("scope", SCOPE).split()
    if resource == FULL_RESOURCE:
        required = {SCOPE, WRITE_SCOPE, EXECUTE_SCOPE}
        allowed = required | {OFFLINE_SCOPE}
    elif resource == ORCH_RESOURCE:
        required = set()
        allowed = {SCOPE, WRITE_SCOPE, EXECUTE_SCOPE, OFFLINE_SCOPE}
    else:
        required = {SCOPE}
        allowed = required | {OFFLINE_SCOPE}
    requested = set(requested_scopes)
    if (len(requested_scopes) != len(requested)
            or not required.issubset(requested)
            or not requested.issubset(allowed)
            or (resource == ORCH_RESOURCE
                and not requested.intersection({SCOPE, WRITE_SCOPE, EXECUTE_SCOPE}))):
        return response_error("invalid_scope")
    scope = " ".join(s for s in (SCOPE, WRITE_SCOPE, EXECUTE_SCOPE, OFFLINE_SCOPE) if s in requested)
    state = q.get("state", "")
    if q.get("response_type") != "code" or resource not in (RESOURCE, ORCH_RESOURCE, FULL_RESOURCE, ISSUER):
        return response_error("invalid_request")
    if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        return response_error("invalid_request")
    if not state or len(state) > 512: return response_error("invalid_request")
    with db() as c:
        client = c.execute("SELECT name,redirect FROM clients WHERE client_id=?", (client_id,)).fetchone()
    if client is None or redirect != client["redirect"] or not valid_redirect(redirect):
        return response_error("unauthorized_client", 403)
    req_id = secrets.token_urlsafe(24)
    APPROVALS[req_id] = {
        "client_id":client_id, "name":client["name"], "redirect":redirect,
        "challenge":challenge, "resource":resource, "scope":scope,
        "state":state, "created":stamp(), "status":"pending"
    }
    if resource == FULL_RESOURCE and PROFILE["direct_local_approval"]:
        # Same-PC Full Access setup: take the browser to the loopback owner
        # page. The owner still reviews and explicitly approves the scopes.
        return RedirectResponse(f"http://127.0.0.1:{ADMIN_PORT}/approve?id={req_id}",
            status_code=303, headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})
    return HTMLResponse(f"""<!doctype html><html><head><meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="3;url=/oauth/status?id=%s"></head><body>
<h2>Awaiting approval on your Windows PC</h2>
<p>Open <b>http://127.0.0.1:{ADMIN_PORT}/</b> on the Windows PC, review this connection and approve it.</p>
<p>This page checks for owner approval automatically.</p>
</body></html>""" % html.escape(req_id, quote=True),
    headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})

async def status(request: Request):
    req_id = request.query_params.get("id", "")
    req = APPROVALS.get(req_id)
    if req is None or stamp() - req["created"] > 180:
        APPROVALS.pop(req_id, None)
        return HTMLResponse("Authorization request expired. Restart connection.", status_code=410)
    if req["status"] == "pending":
        return HTMLResponse(f"""<!doctype html><html><head><meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="3"></head><body>
<h3>Pending approval on Windows</h3>
<p>Visit http://127.0.0.1:{ADMIN_PORT}/ on the Windows computer.</p></body></html>""",
        headers={"Cache-Control":"no-store"})
    APPROVALS.pop(req_id, None)
    if req["status"] == "denied":
        return RedirectResponse(url_with(req["redirect"], {"error":"access_denied","state":req["state"],"iss":ISSUER}))
    code = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO codes (codehash,client_id,redirect,challenge,resource,expires,scope) VALUES (?,?,?,?,?,?,?)", (
            digest(code), req["client_id"], req["redirect"],
            req["challenge"], req["resource"], stamp()+120, req["scope"]))
    return RedirectResponse(url_with(req["redirect"], {"code":code, "state":req["state"], "iss":ISSUER}),
       headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})

async def parse_form(request: Request, maxbytes: int = 8192) -> dict:
    raw = await request.body()
    if len(raw) > maxbytes: return {}
    vals = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    if any(len(v) != 1 for v in vals.values()): return {}
    return {k:v[0] for k,v in vals.items()}

def token_reply(access: str, refresh: str, granted_scope: str):
    return JSONResponse({"access_token":access,"token_type":"Bearer",
        "expires_in":1800,"refresh_token":refresh,"scope":granted_scope},
        headers={"Cache-Control":"no-store","Pragma":"no-cache"})

async def token(request: Request):
    f = await parse_form(request)
    if not f or f.get("client_id", "") == "": return response_error("invalid_request")
    grant = f.get("grant_type")
    client_id = f["client_id"]
    resource = f.get("resource")
    if resource not in (RESOURCE, ORCH_RESOURCE, FULL_RESOURCE, ISSUER): return response_error("invalid_target")
    with db() as c:
        found = c.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not found: return response_error("invalid_client", 401)
        if grant == "authorization_code":
            code = f.get("code", "")
            verifier = f.get("code_verifier", "")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                return response_error("invalid_grant")
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM codes WHERE codehash=?", (digest(code),)).fetchone()
            if row is None or row["expires"] <= stamp() or row["client_id"] != client_id or row["resource"] != resource or row["redirect"] != f.get("redirect_uri") or not hmac.compare_digest(pkce_s256(verifier), row["challenge"]):
                return response_error("invalid_grant")
            granted_scope = row["scope"]
            c.execute("DELETE FROM codes WHERE codehash=?", (digest(code),))
        elif grant == "refresh_token":
            old = f.get("refresh_token","")
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tokens WHERE tokenhash=? AND kind='refresh'", (digest(old),)).fetchone()
            if row is None or row["expires"] <= stamp() or row["client_id"] != client_id or row["resource"] != resource:
                return response_error("invalid_grant")
            granted_scope = row["scope"]
            c.execute("DELETE FROM tokens WHERE tokenhash=?", (digest(old),))
        else:
            return response_error("unsupported_grant_type")
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        c.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?)",
            (digest(access),"access",client_id,resource,granted_scope,stamp()+1800))
        c.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?)",
            (digest(refresh),"refresh",client_id,resource,granted_scope,stamp()+30*86400))
    return token_reply(access, refresh, granted_scope)

def full_challenge() -> Response:
    return Response(status_code=401, headers={
        "WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/full/mcp", scope="mcp:read mcp:write mcp:execute"',
        "Cache-Control":"no-store"})

async def full_mcp_proxy(request: Request):
    return await full.full_proxy(request, __import__(__name__))

async def full_approvals(request: Request):
    return await full.approval_page(request, __import__(__name__))

async def full_decision(request: Request):
    return await full.decision(request, __import__(__name__))

def oauth_challenge(is_orch_route: bool = False) -> Response:
    suffix = "/orch/mcp" if is_orch_route else "/mcp"
    return Response(status_code=401, headers={
        "WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource{suffix}", scope="{SCOPE}"',
        "Cache-Control": "no-store"})

async def mcp_proxy(request: Request):
    is_orch_route = request.url.path == "/orch/mcp"
    values = request.headers.getlist("authorization")
    if len(values) != 1 or not values[0].startswith("Bearer "):
        return oauth_challenge(is_orch_route)
    supplied = values[0][7:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{48,120}", supplied):
        return oauth_challenge(is_orch_route)
    with db() as c:
        tok = c.execute("SELECT * FROM tokens WHERE tokenhash=? AND kind='access'", (digest(supplied),)).fetchone()
        client_exists = c.execute("SELECT 1 FROM clients WHERE client_id=?", (tok["client_id"],)).fetchone() if tok else None
    expected_resources = (ORCH_RESOURCE,) if is_orch_route else (RESOURCE, ISSUER)
    if (tok is None or not client_exists or tok["expires"] <= stamp()
            or tok["resource"] not in expected_resources
            or not (set(tok["scope"].split()) & ({SCOPE, WRITE_SCOPE, EXECUTE_SCOPE}
                                              if is_orch_route else {SCOPE}))):
        return oauth_challenge(is_orch_route)
    if request.headers.get("origin"): return Response(status_code=403)
    if any(k.startswith("mcp-param-") for k in request.headers): return Response(status_code=400)
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow":"POST","Cache-Control":"no-store"})
    raw = await request.body()
    if len(raw) > 1048576: return Response(status_code=413)
    if "application/json" not in request.headers.get("content-type", "").lower():
        return Response(status_code=415)
    try: rpc = json.loads(raw)
    except (ValueError, UnicodeError): return Response(status_code=400)
    if not isinstance(rpc, dict): return Response(status_code=403)
    method = rpc.get("method")
    if method not in ALLOWED_METHODS: return Response(status_code=403)
    tool_name = None
    if method == "tools/call":
        params = rpc.get("params")
        tool_name = params.get("name") if isinstance(params, dict) else None
        allowed = ORCH_ALL if is_orch_route else ALLOWED_TOOLS
        if tool_name not in allowed: return Response(status_code=403)
        needed = ORCH_SCOPE_MAP[tool_name] if is_orch_route else SCOPE
        if needed not in tok["scope"].split():
            return Response(status_code=403, headers={
                "WWW-Authenticate": (f'Bearer error="insufficient_scope", scope="{needed}", '
                                     f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource'
                                     f'{"/orch/mcp" if is_orch_route else "/mcp"}"'),
                "Cache-Control": "no-store"})
    upstream_url = ORCH_BACKEND if is_orch_route else BACKEND
    upstream_body = raw
    upstream_name = tool_name
    if method == "tools/call" and tool_name in ORCH_ALL:
        upstream_url = ORCH_BACKEND
        upstream_name = ORCH_ALL[tool_name]
        mapped_rpc = dict(rpc)
        mapped_params = dict(rpc["params"])
        mapped_params["name"] = upstream_name
        mapped_rpc["params"] = mapped_params
        upstream_body = json.dumps(mapped_rpc, separators=(",", ":")).encode("utf-8")
    if request.headers.get("mcp-protocol-version") == "2026-07-28":
        if request.headers.get("mcp-method") != method: return Response(status_code=400)
        if method == "tools/call" and request.headers.get("mcp-name") != tool_name:
            return Response(status_code=400)
    outgoing = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    for k in ("mcp-protocol-version","mcp-method","mcp-name"):
        if request.headers.get(k): outgoing[k] = request.headers[k]
    if method == "tools/call" and upstream_name is not None and request.headers.get("mcp-name"):
        outgoing["mcp-name"] = upstream_name
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False) as client:
            r = await client.post(upstream_url, content=upstream_body, headers=outgoing)
        if len(r.content) > 4*1024*1024: return Response(status_code=502)
        if method == "server/discover" and r.status_code == 200:
            # Expose only capabilities that this gateway actually permits.
            data = r.json()
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                return Response(status_code=502)
            result["capabilities"] = {"tools": {"listChanged": False}}
            return JSONResponse(data, headers={"Cache-Control":"no-store"})
        if method == "tools/list" and r.status_code == 200:
            data = r.json()
            if "result" not in data or not isinstance(data["result"].get("tools"),list):
                return Response(status_code=502)
            if is_orch_route:
                reverse = {v: k for k, v in ORCH_ALL.items()}
                exposed = []
                for item in data["result"]["tools"]:
                    name = item.get("name") if isinstance(item, dict) else None
                    if name not in reverse:
                        continue
                    entry = dict(item)
                    entry["name"] = reverse[name]
                    entry["title"] = "Orchestrator: " + str(item.get("title") or name)
                    required_scope = ORCH_SCOPE_MAP[entry["name"]]
                    is_read = required_scope == SCOPE
                    is_destructive = entry["name"] in {
                        "orch_task_cancel", "orch_chat_cancel", "orch_chat_close",
                        "orch_task_finalize"}
                    entry["securitySchemes"] = [{"type": "oauth2", "scopes": [required_scope]}]
                    entry["annotations"] = {**(item.get("annotations") or {}),
                                            "readOnlyHint": is_read,
                                            "destructiveHint": is_destructive,
                                            "openWorldHint": not is_read}
                    exposed.append(entry)
                data["result"]["tools"] = exposed
                data["result"].pop("nextCursor", None)
                return JSONResponse(data, headers={"Cache-Control": "no-store"})
            data["result"]["tools"] = [t for t in data["result"]["tools"] if t.get("name") in ACTUATOR_READONLY]
            # Declare tool-level OAuth explicitly for ChatGPT; the gateway already
            # enforces Bearer authentication and this scope for every invocation.
            for tool in data["result"]["tools"]:
                tool["securitySchemes"] = [{"type": "oauth2", "scopes": [SCOPE]}]
                tool["annotations"] = {**(tool.get("annotations") or {}),
                                       "readOnlyHint": True, "destructiveHint": False,
                                       "openWorldHint": False}
            # The Orchestrator has its own local-only MCP listener. Do not expose
            # unapproved tools or forward authenticated client headers to it.
            try:
                orch_headers = dict(outgoing)
                orch_headers.pop("Mcp-Name", None)
                async with httpx.AsyncClient(timeout=12, trust_env=False) as orch_client:
                    orch_result = await orch_client.post(ORCH_BACKEND, content=raw, headers=orch_headers)
                if orch_result.status_code == 200 and len(orch_result.content) <= 4194304:
                    orch_data = orch_result.json()
                    orch_tools = orch_data.get("result", {}).get("tools", [])
                    if isinstance(orch_tools, list):
                        original_to_public = {v: k for k, v in ORCH_READONLY.items()}
                        for tool in orch_tools:
                            if isinstance(tool, dict) and tool.get("name") in original_to_public:
                                exposed = dict(tool)
                                exposed["name"] = original_to_public[tool["name"]]
                                exposed["title"] = "Orchestrator: " + str(tool.get("title") or tool["name"])
                                exposed["securitySchemes"] = [{"type": "oauth2", "scopes": [SCOPE]}]
                                # Avoid overpromising a write permission.
                                annotations = dict(tool.get("annotations") or {})
                                annotations["readOnlyHint"] = True
                                exposed["annotations"] = annotations
                                data["result"]["tools"].append(exposed)
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                # If optional Orchestrator is offline, Actuator remains available.
                pass
            # never advertise a pagination cursor to more tools.
            data["result"].pop("nextCursor", None)
            return JSONResponse(data, headers={"Cache-Control":"no-store"})
        content_type = r.headers.get("content-type","application/json").split(";",1)[0]
        return Response(r.content,status_code=r.status_code,media_type=content_type,
            headers={"Cache-Control":"no-store"})
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return Response(status_code=502)

async def health(request: Request):
    return JSONResponse({"service":"oauth-mcp-gateway","ready":True,
        "issuer": ISSUER, "allowed_tools":sorted(ALLOWED_TOOLS)})

def admin_headers_ok(req: Request) -> bool:
    host = req.headers.get("host", "")
    origin = req.headers.get("origin","")
    return host == f"127.0.0.1:{ADMIN_PORT}" and (not origin or origin == f"http://127.0.0.1:{ADMIN_PORT}")

async def admin_approve(request: Request):
    if not admin_headers_ok(request): return Response(status_code=403)
    req_id = request.query_params.get("id", "")
    pending = APPROVALS.get(req_id)
    if (pending is None or pending["status"] != "pending"
            or stamp() - pending["created"] > 180
            or pending["resource"] != FULL_RESOURCE):
        return HTMLResponse("Approval request expired. Restart the connection in ChatGPT.", status_code=410)
    safe_id = html.escape(req_id, quote=True)
    scope_chips = "".join(f"<span class='pill'>{html.escape(scope)}</span>" for scope in pending["scope"].split())
    body = ("<header><div><div class='brand'><span class='logo'>M</span> MCP GATEWAY</div>"
        "<h1>Approve this connection</h1><p class='lead'>Review the permissions before continuing to ChatGPT.</p>"
        "</div><span class='local'>LOCAL APPROVAL</span></header>"
        "<section class='card approval'><div class='section-top'><div><h2>Connection request</h2>"
        "<p>Only approve a connection you just started.</p></div></div>"
        f"<div class='field'><span class='field-label'>Client</span><strong>{html.escape(pending['name'])}</strong></div>"
        f"<div class='field'><span class='field-label'>Resource</span><code>{html.escape(pending['resource'])}</code></div>"
        f"<div class='field'><span class='field-label'>Requested scopes</span>{scope_chips}</div>"
        "<form method='post' action='/decision' class='actions'>"
        f"<input type='hidden' name='id' value='{safe_id}'>"
        f"<input type='hidden' name='continue' value='{safe_id}'>"
        f"<input type='hidden' name='csrf' value='{html.escape(CSRF, quote=True)}'>"
        "<button class='primary' name='choice' value='approve'>Approve and continue</button>"
        "<button name='choice' value='deny'>Deny</button></form></section>"
        "<p class='note'>This page is available only on the Windows computer running the gateway.</p>")
    return HTMLResponse(owner_ui.render("Approve connection | MCP Gateway", body, "no-referrer"),
        headers={"Cache-Control":"no-store", "X-Frame-Options":"DENY", "Referrer-Policy":"no-referrer"})

async def admin_home(request: Request):
    if not admin_headers_ok(request): return Response(status_code=403)
    pending_rows = []
    for req_id, a in list(APPROVALS.items()):
        if a["status"] != "pending" or stamp()-a["created"] > 180: continue
        chips = "".join(f"<span class='pill'>{html.escape(scope)}</span>" for scope in a["scope"].split())
        pending_rows.append(
            "<div class='row'><div class='row-info'>"
            f"<div class='row-title'>{html.escape(a['name'])}</div>"
            f"<div class='meta'>Resource: <code>{html.escape(a['resource'])}</code></div>"
            f"<div class='meta'>{chips}</div></div>"
            "<form method='post' action='/decision' class='actions'>"
            f"<input type='hidden' name='id' value='{html.escape(req_id, quote=True)}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(CSRF, quote=True)}'>"
            "<button class='primary' name='choice' value='approve'>Approve requested scopes</button>"
            "<button name='choice' value='deny'>Deny</button></form></div>")
    with db() as c:
        clients = c.execute(
            "SELECT c.client_id,c.name, "
            "SUM(CASE WHEN t.expires > ? THEN 1 ELSE 0 END) AS active_count "
            "FROM clients c LEFT JOIN tokens t ON t.client_id = c.client_id "
            "GROUP BY c.client_id,c.name ORDER BY c.created DESC LIMIT 100",
            (stamp(),)).fetchall()
    active_rows, inactive_rows = [], []
    for client in clients:
        client_id = html.escape(client["client_id"], quote=True)
        safe_name = html.escape(client["name"])
        count = int(client["active_count"] or 0)
        badge = (f"<span class='pill green'>{count} active token{'s' if count != 1 else ''}</span>"
                 if count else "<span class='pill dim'>No active tokens</span>")
        row = ("<div class='row'><div class='row-info'>"
            f"<div class='row-title'>{safe_name}</div>"
            f"<div class='meta'>Client ending in <code>{client_id[-8:]}</code> {badge}</div></div>"
            "<form method='post' action='/revoke-connection' class='actions'>"
            f"<input type='hidden' name='client_id' value='{client_id}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(CSRF, quote=True)}'>"
            "<button class='danger' name='choice' value='revoke'>Revoke this connection</button>"
            "</form></div>")
        (active_rows if count else inactive_rows).append(row)
    pending_html = ("<div class='list'>" + "".join(pending_rows) + "</div>" if pending_rows
                    else "<div class='empty'><strong>No pending requests</strong>Start a connection in ChatGPT to review it here.</div>")
    active_html = ("<div class='list'>" + "".join(active_rows) + "</div>" if active_rows
                   else "<div class='empty'><strong>No active connections</strong>Registered clients without active tokens appear below.</div>")
    inactive_html = ("<details class='details'><summary>Inactive clients (" + str(len(inactive_rows)) + ")</summary>"
                     "<div class='list'>" + "".join(inactive_rows) + "</div></details>" if inactive_rows else "")
    body = ("<header><div><div class='brand'><span class='logo'>M</span> MCP GATEWAY</div>"
        "<h1>Owner console</h1><p class='lead'>Review requests and manage ChatGPT connections on this PC.</p>"
        "</div><span class='local'>LOCAL ONLY &middot; 127.0.0.1</span></header>"
        "<section class='card'><div class='section-top'><div><h2>Pending approval</h2>"
        "<p>Approve only a connection you just initiated in ChatGPT.</p></div>"
        f"<span class='count'>{len(pending_rows)}</span></div>{pending_html}</section>"
        "<section class='card'><div class='section-top'><div><h2>Connected clients</h2>"
        "<p>Review active tokens or revoke a client.</p></div>"
        f"<span class='count'>{len(clients)}</span></div>{active_html}{inactive_html}"
        "<p class='note'>Revocation blocks future requests and refresh tokens. It does not stop calls already running.</p>"
        "</section><a class='link' href='/'>Refresh status</a>")
    return HTMLResponse(owner_ui.render("Owner console | MCP Gateway", body),
        headers={"Cache-Control":"no-store","X-Frame-Options":"DENY","Referrer-Policy":"same-origin"})

async def admin_decision(request: Request):
    if not admin_headers_ok(request) or request.headers.get("origin") != f"http://127.0.0.1:{ADMIN_PORT}":
        return Response(status_code=403)
    fields = await parse_form(request, 4096)
    if not hmac.compare_digest(fields.get("csrf",""), CSRF): return Response(status_code=403)
    req_id = fields.get("id","")
    pending = APPROVALS.get(req_id)
    if pending is None or pending["status"]!="pending" or stamp()-pending["created"] > 180:
        return Response(status_code=410)
    choice = fields.get("choice")
    if choice not in ("approve","deny"): return Response(status_code=400)
    pending["status"] = "approved" if choice == "approve" else "denied"
    if fields.get("continue") == req_id and pending["resource"] == FULL_RESOURCE:
        return RedirectResponse(f"{ISSUER}/oauth/status?id={req_id}", status_code=303,
            headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})
    return RedirectResponse("/", status_code=303)

async def admin_revoke_connection(request: Request):
    if not admin_headers_ok(request) or request.headers.get("origin") != f"http://127.0.0.1:{ADMIN_PORT}":
        return Response(status_code=403)
    fields = await parse_form(request, 4096)
    if not hmac.compare_digest(fields.get("csrf", ""), CSRF):
        return Response(status_code=403)
    if fields.get("choice") != "revoke":
        return Response(status_code=400)
    client_id = fields.get("client_id", "")
    if not re.fullmatch(r"mcp_[A-Za-z0-9_-]{20,60}", client_id):
        return Response(status_code=400)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        found = c.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if found is None: return Response(status_code=404)
        c.execute("DELETE FROM tokens WHERE client_id=?", (client_id,))
        c.execute("DELETE FROM codes WHERE client_id=?", (client_id,))
        c.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
    for key, pending in list(APPROVALS.items()):
        if pending.get("client_id") == client_id:
            APPROVALS.pop(key, None)
    for key, action in list(full.PENDING.items()):
        if action.get("client_id") == client_id:
            full.PENDING.pop(key, None)
    return RedirectResponse("/", status_code=303)

public_app = Starlette(routes=[
    Route("/.well-known/oauth-protected-resource", resource_meta, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/mcp", resource_meta, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/orch/mcp", resource_meta, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/full/mcp", resource_meta, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", oauth_meta, methods=["GET"]),
    Route("/oauth/register", register, methods=["POST"]),
    Route("/oauth/authorize", authorize, methods=["GET"]),
    Route("/oauth/status", status, methods=["GET"]),
    Route("/oauth/token", token, methods=["POST"]),
    Route("/mcp", mcp_proxy, methods=["POST","GET"]),
    Route("/orch/mcp", mcp_proxy, methods=["POST","GET"]),
    Route("/full/mcp", full_mcp_proxy, methods=["POST","GET"]),
    Route("/healthz", health, methods=["GET"])
])
admin_app = Starlette(routes=[
    Route("/", admin_home, methods=["GET"]),
    Route("/approve", admin_approve, methods=["GET"]),
    Route("/decision", admin_decision, methods=["POST"]),
    Route("/revoke-connection", admin_revoke_connection, methods=["POST"]),
    Route("/full-approvals", full_approvals, methods=["GET"]),
    Route("/full-decision", full_decision, methods=["POST"])
])

# Minimal privacy-preserving diagnostics. Never record bearer values, payloads, URLs with
# querystrings, client identifiers, redirect codes, cookies, or HTTP Authorization headers.
AUDIT_LOG = ROOT / "gateway-mcp-audit.jsonl"

class SafeAuditASGI:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        observed = path in ("/mcp", "/orch/mcp", "/full/mcp", "/oauth/token", "/oauth/authorize", "/oauth/register")
        if not observed:
            return await self.app(scope, receive, send)
        hdrs = {k.lower():v for k,v in scope.get("headers", [])}
        def safe_field(name):
            value = hdrs.get(name.encode("ascii"), b"").decode("ascii", "replace")
            return value[:64] if re.fullmatch(r"[A-Za-z0-9_./-]{0,64}", value) else "invalid"
        record = {"ts": int(time.time()), "path":path, "http_method":scope.get("method",""),
                  "mcp_method":safe_field("mcp-method") if path in ("/mcp", "/orch/mcp", "/full/mcp") else "",
                  "protocol_version":safe_field("mcp-protocol-version") if path in ("/mcp", "/orch/mcp", "/full/mcp") else "",
                  "bearer_present":hdrs.get(b"authorization",b"").startswith(b"Bearer "),
                  "status":0}
        async def traced_send(message):
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
            await send(message)
        try:
            return await self.app(scope, receive, traced_send)
        finally:
            try:
                # Bound diagnostic disk consumption without exposing credential values.
                if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size >= 5*1024*1024:
                    AUDIT_LOG.replace(ROOT / "gateway-mcp-audit.previous.jsonl")
                with AUDIT_LOG.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, separators=(",", ":"))+"\n")
            except OSError:
                pass

# Defense in depth: all public MCP requests traverse a fail-closed guard.
guarded_public_app = RequestGuard(public_app)
audited_public_app = SafeAuditASGI(guarded_public_app)

async def run():
    initialize_db()
    srv1=uvicorn.Server(uvicorn.Config(audited_public_app, host="127.0.0.1",port=PUBLIC_PORT,
        log_level="warning", access_log=False, server_header=False,
        limit_concurrency=128, timeout_keep_alive=5))
    srv2=uvicorn.Server(uvicorn.Config(RequestGuard(admin_app), host="127.0.0.1",port=ADMIN_PORT,
        log_level="warning", access_log=False, server_header=False))
    await asyncio.gather(srv1.serve(),srv2.serve())

if __name__ == "__main__":
    asyncio.run(run())
