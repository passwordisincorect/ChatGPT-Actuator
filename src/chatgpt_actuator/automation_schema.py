"""Compare exported schemas, not just tool names. No permission changes."""
import hashlib,inspect,json
from .tool_policy import authorize

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def validate_server(server,functions):
    for tool in server._tool_manager.list_tools():
        if not tool.name.startswith(('uia_','dom_')):continue
        signature=inspect.signature(functions[tool.name]);schema=tool.parameters
        expected={n for n,p in signature.parameters.items() if p.default is inspect.Parameter.empty}
        if set(schema.get('properties',{}))!=set(signature.parameters) or set(schema.get('required',[]))!=expected:
            raise RuntimeError('Automation schema differs from registered signature: '+tool.name)

def report(server,principal,connector_schemas=None):
    exported=[]
    for tool in server._tool_manager.list_tools():
        if not tool.name.startswith(('uia_','dom_')):continue
        try:authorize(tool.name,principal)
        except PermissionError:continue
        exported.append({'name':tool.name,'inputSchema':tool.parameters})
    exported.sort(key=lambda x:x['name'])
    result={'schema_version':1,'schema_fingerprint':digest(exported),'tools':exported,
        'source':'registered_tools/list','application_catalog_verified':False,
        'next_action':'Client must re-fetch tools/list after a runtime update; OAuth reauthorization does not repair stale schemas'}
    if connector_schemas is not None:
        if not isinstance(connector_schemas,list) or len(connector_schemas)>128 or len(json.dumps(connector_schemas).encode())>262144:raise ValueError('Invalid connector schemas')
        observed={}
        for item in connector_schemas:
            if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not isinstance(item.get('inputSchema'),dict):raise ValueError('Provide name and inputSchema per tool')
            name=item['name']
            for prefix in ('mcp__codex_apps__actuator_actuator__','actuator.actuator__','actuator__'):
                if name.startswith(prefix):name=name[len(prefix):];break
            if len(name)>128 or name in observed:raise ValueError('Invalid or duplicate tool name')
            observed[name]=item['inputSchema']
        expected={item['name']:item['inputSchema'] for item in exported}
        changed=sorted(name for name in expected.keys()&observed.keys() if digest(expected[name])!=digest(observed[name]))
        missing=sorted(expected.keys()-observed.keys());unexpected=sorted(observed.keys()-expected.keys())
        result['comparison']={'source':'caller_supplied_schemas','state':'mismatch' if changed or missing or unexpected else 'matches',
            'changed_schema':changed,'missing':missing,'unexpected':unexpected,'application_verified':False}
    return result
