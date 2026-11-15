"""Owner-local activation verifies RPC and policy, not just HTTP availability."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import httpx
from .tool_policy import BACKGROUND_BLOCKED_TOOLS

def decode_rpc(response):
    response.raise_for_status()
    if 'text/event-stream' in response.headers.get('content-type',''):
        for line in response.text.splitlines():
            if line.startswith('data:'):
                value=json.loads(line[5:].strip())
                if 'result' in value or 'error' in value:return value
        raise ValueError('No RPC result in event stream')
    return response.json()

def verify_inventory(names):
    if not names or names & BACKGROUND_BLOCKED_TOOLS:raise RuntimeError('Missing inventory or forbidden tool exposed')
    if not {'security_policy_info','capability_readiness','command_profile_run','work_session_open'}<=names:
        raise RuntimeError('Incomplete background runtime')

def verify_backend(url,version):
    headers={'accept':'application/json, text/event-stream'}
    with httpx.Client(timeout=5,trust_env=False) as c:
        def rpc(method,params):
            data=decode_rpc(c.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params}))
            if 'error' in data:raise RuntimeError('Backend RPC failed')
            return data['result']
        names={t['name'] for t in rpc('tools/list',{}).get('tools',[])};verify_inventory(names)
        result=rpc('tools/call',{'name':'security_policy_info','arguments':{}})
        if result.get('isError'):raise RuntimeError('Runtime policy call failed')
        policy=json.loads(next(x['text'] for x in result['content'] if x['type']=='text'))
        if policy.get('runtime_version')!=version or policy.get('execution_policy') not in {'background_only','scoped_desktop_patterns'} or policy.get('physical_input')!='removed':raise RuntimeError('Running version/policy mismatch')
        denied=decode_rpc(c.post(url,headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'mouse_position','arguments':{}}}))
        if 'error' not in denied and not denied.get('result',{}).get('isError'):raise RuntimeError('Removed read-only tool callable')
    return {'backend_rpc':'verified','registered_tools':len(names),'version':version}

def verify_gateway(directory,url):
    directory=Path(directory);sys.path.insert(0,str(directory))
    try:
        spec=importlib.util.spec_from_file_location('owner_gateway_health',directory/'gateway_full.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        token=module.identity_context.set({'owner':'owner-update-health','scopes':['mcp:read','mcp:write','mcp:execute'],'session':'default'})
        try:mapping=asyncio.run(module.tool_map(type('Backend',(),{'BACKEND':url})(),{'id':1},{'Content-Type':'application/json','Accept':'application/json, text/event-stream'}))
        finally:module.identity_context.reset(token)
        verify_inventory({name.removeprefix('actuator__') for name in mapping})
        with httpx.Client(timeout=3,trust_env=False) as c:c.get('http://127.0.0.1:8188/healthz').raise_for_status()
        return {'gateway_signed_backend_rpc':'verified','gateway_http':'verified'}
    finally:sys.path.remove(str(directory))
