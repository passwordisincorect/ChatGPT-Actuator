"""Loopback diagnostics distinguish a listening process from successful MCP."""
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx
import psutil

async def probe_mcp(url):
    u=urlsplit(url)
    if u.scheme!="http" or u.hostname not in {"127.0.0.1","localhost","::1"} or u.path!="/mcp":
        raise ValueError("Diagnostic MCP probe must target HTTP loopback /mcp")
    headers={"accept":"application/json, text/event-stream"}
    result={"url":url,"http_status":None,"mcp_list_ok":False,"tool_count":None,
            "readonly_call_ok":False}
    try:
        async with httpx.AsyncClient(timeout=5,trust_env=False) as c:
            response=await c.post(url,json={"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}},headers=headers)
            result["http_status"]=response.status_code
            if response.status_code!=200: return result
            from .update_health import decode_rpc
            data=decode_rpc(response)
            tools=data.get("result",{}).get("tools")
            if not isinstance(tools,list): return result
            result.update(mcp_list_ok=True,tool_count=len(tools))
            names={t.get("name") for t in tools if isinstance(t,dict)}
            if "routing_policy_info" in names:
                call=await c.post(url,json={"jsonrpc":"2.0","id":2,"method":"tools/call",
                    "params":{"name":"routing_policy_info","arguments":{}}},headers=headers)
                payload=decode_rpc(call) if call.status_code==200 else {}
                result["readonly_call_ok"]=isinstance(payload.get("result"),dict) and not payload["result"].get("isError",False)
    except (httpx.HTTPError,ValueError):
        result["error"]="Loopback HTTP/MCP probe failed"
    return result

async def snapshot(runtime, connector_error=None):
    listeners=[]
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status==psutil.CONN_LISTEN and c.laddr.port in {8090,8188,8189}:
                listeners.append({"host":c.laddr.ip,"port":c.laddr.port,"pid":c.pid})
    except (psutil.AccessDenied,OSError):
        pass
    process_counts={"tunnel_client":0,"tailscale":0}
    for p in psutil.process_iter(["name"]):
        name=(p.info["name"] or "").lower()
        if "tunnel" in name: process_counts["tunnel_client"]+=1
        if "tailscale" in name: process_counts["tailscale"]+=1
    from .evidence import Evidence
    from . import __version__
    from .connection_observation import observation,FRESH_SECONDS
    current=observation(runtime)
    evidence=Evidence(runtime.config_path.parent/'runtime-evidence.sqlite3').snapshot(__version__,max_age=FRESH_SECONDS) if hasattr(runtime,'config_path') else {}
    from .tunnel_diagnostics import metadata_report, layers
    home=runtime.config_path.parent.parent if hasattr(runtime,'config_path') else None
    metadata=metadata_report(home/'deployment/tunnel-diagnostics.json',settings_path=home/'deployment/tunnel-organizations.json') if home else {'state':'unverified'}
    backend=await probe_mcp('http://127.0.0.1:8090/mcp')
    fresh=evidence
    report=layers(backend,fresh,metadata)
    from .update_history import UpdateHistory
    from .runtime_integrity import verify_active
    integrity=False
    if home:
        try:verify_active(home,__version__);integrity=True
        except (OSError,ValueError,KeyError):pass
    history=UpdateHistory(home/'deployment/update-history.sqlite3').summary(__version__,integrity_verified=integrity,local_mcp_verified=backend.get('readonly_call_ok') is True) if home else {}
    from .deployment_status import states
    deployment=states(integrity,backend.get('readonly_call_ok') is True,report['openai_chatgpt_mcp']['state'],report['tailscale_authenticated_mcp']['state'])
    from .owner_launcher import LaunchJournal
    launches=LaunchJournal(home/'deployment/launch-history.sqlite3').latest(5) if home else []
    from .oauth_session import snapshot as oauth_snapshot
    oauth=oauth_snapshot(home,verified=report['tailscale_authenticated_mcp']['state']=='verified') if home else {'state':'unavailable'}
    from .post_update_health import HealthHistory
    health=HealthHistory(home/'deployment/post-update-health.sqlite3').summary(__version__) if home else {'state':'not_scheduled'}
    from .connector_binding import binding
    return {'connection_observation':current,'evidence_freshness_seconds':FRESH_SECONDS,'connector_binding':binding(backend,connector_error,current),'post_update_health':health,'oauth_session':oauth,'deployment':deployment,'launches':launches,'update':history,'layers':report,'runtime_version':__version__,'evidence':evidence,"checked_at":datetime.now(timezone.utc).isoformat(),"listeners":listeners,
            "process_counts":process_counts,"backend":backend,
            "openai_tunnel":{"authenticated_e2e":report["openai_chatgpt_mcp"]["state"]},
            "tailscale_funnel":{"authenticated_mcp":report["tailscale_authenticated_mcp"]["state"],"oauth_chatgpt_e2e":"unverified"},
            "note":"Process existence and local MCP success do not establish public OAuth or ChatGPT connectivity."}
