"""Export a curated support receipt, not raw logs, configuration or credential stores."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,re,secrets,zipfile,math
STATES={'verified','stale','unverified','unavailable','awaiting_remote_calls','awaiting_remote_call','awaiting_actual_call','applied','observing','failed','succeeded','pending','superseded','not_scheduled','refresh_needed','refresh_failed','reconnect_required','not_connected','running','skipped','busy','selected_unverified'}
ERRORS={'RuntimeError','ValueError','PermissionError','TimeoutError','TimeoutExpired','OSError','FileNotFoundError','ConnectionError','ConnectError','ReadTimeout','OperationalError','ConfigError','HTTPStatusError','CalledProcessError','ModuleNotFoundError','ImportError'}
def state(value):return value if isinstance(value,str) and value in STATES else 'unverified'
def version(value):return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,79}',value) else 'unverified'
def timestamp(value):return value if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<10**12 else None
def failure(value):
    if not isinstance(value,dict) or not value:return None
    error=value.get('error') or {};kind=error.get('type') or value.get('type') or value.get('error_type')
    step=value.get('step') or value.get('phase')
    return {'step':step if isinstance(step,str) and step in {'integrity','supervisor','local_services','control_center','openai_start','local_mcp','gateway_mcp','owned_tunnel','resume_study','stop','attempt','unified_restart','automatic_rollback','package_preflight','baseline','apply_files','activation','post_activation','windows_preflight','observation_schedule'} else 'other','error_type':kind if isinstance(kind,str) and kind in ERRORS|{'ObservationMissed'} else 'unclassified'}
def curated(diagnostics):
    deployment=diagnostics.get('deployment',{});oauth=diagnostics.get('oauth_session',{});health=diagnostics.get('post_update_health',{})
    receipt={'runtime_version':version(diagnostics.get('runtime_version')),'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'deployment':{'state':state(deployment.get('deployment')),'connections':state(deployment.get('connections')),'complete':deployment.get('complete') is True,'links':{name:state(deployment.get('links',{}).get(name)) for name in ('openai','tailscale')}},
        'layers':{name:{'state':state(value.get('state'))} for name,value in diagnostics.get('layers',{}).items() if name in {'local_http','local_mcp_list','local_mcp_call','openai_chatgpt_mcp','tailscale_authenticated_mcp'} and isinstance(value,dict)},
        'oauth':{'state':state(oauth.get('state')),'actual_mcp_verified':oauth.get('actual_mcp_verified') is True,'counts':{k:v for k,v in oauth.get('counts',{}).items() if k in {'access_total','access_live','refresh_total','refresh_live'} and type(v) is int and 0<=v<10**7}},
        'post_update_health':{'state':state(health.get('state')),'checkpoints':[{'delay_seconds':c.get('delay_seconds'),'state':state(c.get('state')),'failure':failure(c.get('details'))} for c in health.get('checkpoints',[]) if c.get('delay_seconds') in {30,120,600}]},
        'launches':[{'version':version(row.get('version')),'action':row.get('action') if row.get('action') in {'start','restart','restore','status'} else 'other','state':state(row.get('state')),'started_at':timestamp(row.get('started_at')),'first_failure':failure(row.get('first_failure'))} for row in diagnostics.get('launches',[])[:10]],
        'updates':[{'target_version':version(row.get('target_version')),'state':state(row.get('state')),'started_at':timestamp(row.get('started_at')),'checks':{k:v for k,v in row.get('checks',{}).items() if k in {'windows_tests','private_desktop','headless_dom','local_mcp','gateway_rpc','openai_mcp','tailscale_mcp','compiler','service_start_stop'} and type(v) is bool}} for row in diagnostics.get('update',{}).get('attempts',[])[:10]],
        'excluded':['raw_logs','configuration','credentials','tokens','client_identity','request_bodies','command_output','user_paths']}
    last=oauth.get('last_request') or {}
    if last.get('grant_type') in {'refresh_token','authorization_code'}:
        receipt['oauth']['last_request']={'grant_type':last['grant_type'],'state':state(last.get('state')),'error_code':last.get('error_code') if last.get('error_code') in {'invalid_request','invalid_target','invalid_client','invalid_grant','unsupported_grant_type','server_error','temporarily_unavailable'} else None,'checked_at':timestamp(last.get('checked_at'))}
    count=diagnostics.get('backend',{}).get('tool_count')
    if type(count) is int:receipt['tool_count']=count
    receipt['findings']=[]
    if receipt['post_update_health']['state']=='not_scheduled':
        receipt['findings'].append({'layer':'post_update_health','code':'observation_missing',
            'next_action':'Schedule the existing read-only owner observation for this activation.'})
    for name,layer in receipt['layers'].items():
        if layer['state'] in {'stale','unverified','failed'}:
            receipt['findings'].append({'layer':name,'code':'fresh_evidence_required',
                'next_action':'Run a read-only check through this exact connection; retain its timestamp and runtime version.'})
    return receipt
def export_bundle(home,diagnostics):
    home=Path(home).resolve(strict=True);directory=home/'diagnostics';directory.mkdir(exist_ok=True)
    if directory.resolve().parent!=home:raise ValueError('Diagnostics directory escaped owner home')
    receipt=curated(diagnostics);data=json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8')
    if len(data)>512*1024:raise ValueError('Diagnostic receipt exceeds limit')
    name='Actuator-Diagnostics-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)+'.zip'
    target=directory/name;temporary=target.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'x',zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('diagnostics.json',data)
        bundle.writestr('README.txt','Curated Actuator support receipt. No raw logs, configuration, tokens or credentials are included. Connection verification remains separate from local activation.\n')
        bundle.writestr('SHA256.txt',hashlib.sha256(data).hexdigest()+'  diagnostics.json\n')
    temporary.replace(target)
    return {'state':'exported','note':'Gói chẩn đoán: '+str(target),'path':str(target)}
