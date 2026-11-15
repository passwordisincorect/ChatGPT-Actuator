"""Deterministic tool inventory and permission evidence; never grants access."""
import hashlib
import json
from .tool_policy import authorize, scope_for, required_scopes, BACKGROUND_BLOCKED_TOOLS

def fingerprint(tools):
    canonical = [{'name': t['name'], 'inputSchema': t.get('inputSchema', {}),
                  'description': t.get('description', '')} for t in tools]
    canonical.sort(key=lambda t: t['name'])
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()

def catalog(runtime, server, principal, *, browser_ready=False, connector_names=None, connector_tools=None):
    tools = []
    for tool in server._tool_manager.list_tools():
        tools.append({'name': tool.name, 'inputSchema': tool.parameters,
                      'description': tool.description or ''})
    names = {t['name'] for t in tools}
    if len(names) != len(tools):
        raise ValueError('Duplicate runtime tool names')
    exported = None
    if connector_names is not None:
        if not isinstance(connector_names, list) or len(connector_names) > 256:
            raise ValueError('connector_names must contain at most 256 names')
        if any(not isinstance(n, str) or len(n) > 128 for n in connector_names):
            raise ValueError('Invalid connector tool name')
        def normalize(n):
            for prefix in ('mcp__codex_apps__actuator_actuator__','actuator.actuator__','actuator__'):
                if n.startswith(prefix):return n[len(prefix):]
            return n
        exported = {normalize(n) for n in connector_names}
    entries, visible = [], []
    for tool in sorted(tools, key=lambda t: t['name']):
        name = tool['name']
        state, reason, action = 'registered_not_probed', 'No live provider probe performed', 'Run a scoped readiness check'
        try:
            authorize(name, principal)
        except PermissionError:
            if name in BACKGROUND_BLOCKED_TOOLS:
                state, reason, action = 'blocked_by_policy', 'Immutable background policy', 'Use headless DOM or read-only UIA'
            elif any(scope not in principal.scopes for scope in required_scopes(name)):
                state, reason, action = 'missing_scope', 'OAuth scope not granted', 'Owner may explicitly approve the required scope'
            else:
                state, reason, action = 'approval_required', 'Local owner approval required', 'Review the local execution policy'
        else:
            visible.append(tool)
            if name in {'uia_app_open','uia_desktop_attach'} and not runtime.raw_config().get('security',{}).get('desktop_uia',{}).get('enabled'):
                state,reason,action='blocked_by_policy','Desktop UIA disabled by owner policy','Owner can enable selected desktop UIA'
            if name=='dom_browser_attach' and not getattr(runtime.config.browser_dom,'visible_browser',False):
                state,reason,action='blocked_by_policy','Desktop browser policy disabled','Owner can enable selected DOM browser attachment'
            if name.startswith('dom_') and not browser_ready:
                state, reason, action = 'backend_unavailable', 'Browser configuration or dependency missing', 'Check browser configuration'
        entries.append({'name': name, 'required_scope': scope_for(name),
                        'required_scopes': list(required_scopes(name)), 'state': state,
                        'reason': reason, 'next_action': action})
    result = {'schema_version': 1, 'registered_count': len(tools), 'authorized_count': len(visible),
              'runtime_fingerprint': fingerprint(tools), 'authorized_fingerprint': fingerprint(visible),
              'scopes': sorted(principal.scopes), 'tools': entries,
              'catalog_refresh':{'fingerprint_includes_schema_and_description':True,'list_changed_notifications':False,
                                  'next_action':'Client must re-fetch tools/list; matching names do not prove application dispatch'},
              'readiness_note': 'Registration and authorization do not prove provider or connector readiness.'}
    if exported is not None:
        expected = {t['name'] for t in visible}
        result['connector_comparison'] = {'source': 'caller_supplied_names',
            'state': 'matches' if expected == exported else 'mismatch',
            'missing': sorted(expected - exported), 'unexpected': sorted(exported - expected),
            'next_action': 'Refresh the connector tool catalog; do not widen OAuth scopes to repair a catalog mismatch.'}
    if connector_tools is not None:
        if not isinstance(connector_tools,list) or len(connector_tools)>256:
            raise ValueError('connector_tools must contain at most 256 tools')
        supplied=[]
        for entry in connector_tools:
            if not isinstance(entry,dict) or not isinstance(entry.get('name'),str):
                raise ValueError('Invalid connector tool')
            name=entry['name']
            for prefix in ('mcp__codex_apps__actuator_actuator__','actuator.actuator__','actuator__'):
                if name.startswith(prefix):name=name[len(prefix):];break
            if len(name)>128 or not name or not isinstance(entry.get('inputSchema'),dict) or not isinstance(entry.get('description',''),str):
                raise ValueError('Invalid connector schema')
            supplied.append({'name':name,'inputSchema':entry['inputSchema'],'description':entry.get('description','')})
        if len(json.dumps(supplied,ensure_ascii=False,allow_nan=False).encode())>2*1024*1024:
            raise ValueError('Connector schema evidence exceeds limit')
        remote={t['name']:t for t in supplied}
        if len(remote)!=len(supplied):raise ValueError('Duplicate normalized connector tool names')
        expected={t['name']:t for t in visible}
        changed=sorted(n for n in expected.keys()&remote.keys()
                       if fingerprint([expected[n]])!=fingerprint([remote[n]]))
        result['connector_schema_comparison']={'source':'caller_supplied_schemas',
            'state':'matches' if expected.keys()==remote.keys() and not changed else 'mismatch',
            'missing':sorted(expected.keys()-remote.keys()),'unexpected':sorted(remote.keys()-expected.keys()),
            'changed':changed,'connector_fingerprint':fingerprint(supplied),
            'next_action':'Re-fetch tools/list and repeat a read-only call through the intended connector; schema equality alone does not prove delivery.'}
    return result
