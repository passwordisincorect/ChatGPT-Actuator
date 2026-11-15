"""Bounded, read-only status probes for the shared local dashboard."""
import json
import time
from threading import Lock
import httpx
from .tool_catalog import fingerprint

_lock = Lock()
_cache = None
_expires = 0

def inventory(port):
    meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
            'io.modelcontextprotocol/clientInfo': {'name': 'OwnerDashboard', 'version': '1'},
            'io.modelcontextprotocol/clientCapabilities': {}}
    response = httpx.post(f'http://127.0.0.1:{port}/mcp',
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': meta}},
        headers={'Accept': 'application/json, text/event-stream',
                 'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': 'tools/list'},
        timeout=2, trust_env=False)
    response.raise_for_status()
    if len(response.content) > 4 * 1024 * 1024:
        raise ValueError('Inventory response exceeded limit')
    result = response.json()['result']
    if result.get('nextCursor'):
        raise ValueError('Incomplete inventory; pagination required')
    tools = result['tools']
    if not isinstance(tools, list) or len(tools) > 256:
        raise ValueError('Invalid inventory')
    return tools

def snapshot():
    global _cache, _expires
    with _lock:
        now = time.monotonic()
        if _cache is not None and now < _expires:
            return json.loads(json.dumps(_cache))
        rows = {}
        from .tool_policy import scope_for, BACKGROUND_BLOCKED_TOOLS
        for name, port in (('actuator', 8090), ('orchestrator', 8092)):
            try:
                tools = inventory(port)
                entries = []
                for tool in tools:
                    row = {'name': tool['name'], 'state': 'registered_not_probed'}
                    if name == 'actuator':
                        row['required_scope'] = scope_for(tool['name'])
                        if tool['name'] in BACKGROUND_BLOCKED_TOOLS:
                            row['state'] = 'blocked_by_policy'
                    entries.append(row)
                rows[name] = {'state': 'inventory_observed', 'count': len(tools),
                              'fingerprint': fingerprint(tools), 'tools': entries}
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                rows[name] = {'state': 'unavailable', 'count': None, 'code': 'INVENTORY_UNAVAILABLE'}
        try:
            response=httpx.post('http://127.0.0.1:8090/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'capability_readiness','arguments':{}}},
                headers={'Accept':'application/json, text/event-stream'},timeout=2,trust_env=False)
            from .update_health import decode_rpc
            payload=decode_rpc(response)['result']
            if payload.get('isError'):raise ValueError('Readiness unavailable')
            value=payload.get('structuredContent') or json.loads(payload['content'][0]['text'])
            rows['provider_groups']=value.get('provider_groups',[])
            from .schema_status import describe
            rows['schema_status']=describe(value.get('runtime_version','unknown'),inventory(8090))
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            rows['provider_groups']=[]
            rows['schema_status']={'runtime_version':'unknown','backend_catalog_state':'unavailable','application_catalog_state':'unverified'}
        rows['checked_at'] = time.time()
        rows['scope_context'] = 'local_owner_inventory'
        rows['connector_verified'] = False
        rows['note'] = 'Local inventory does not prove OAuth scopes, connector discovery, or worker readiness.'
        _cache, _expires = rows, time.monotonic() + 5
        return json.loads(json.dumps(rows))
