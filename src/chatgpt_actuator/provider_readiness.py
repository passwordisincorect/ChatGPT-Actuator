"""Versioned owner test evidence, distinct from dependency and scope checks."""
import hashlib,json,time
from pathlib import Path

def family(name):
    if name.startswith('eda_altium_'):return 'eda_altium'
    if name=='uia_control_focus':return 'uia_control'
    if name in {'uia_control_window','uia_control_scroll','uia_control_reveal','uia_desktop_windows','uia_desktop_recover','uia_app_open','uia_desktop_attach','uia_desktop_release'}:return 'uia_control'
    if name in {'uia_workspace_profiles','uia_workspace_open','uia_workspace_windows','uia_workspace_close','uia_control_bind','uia_control_snapshot','uia_control_find','uia_control_state','uia_invoke','uia_set_value','uia_toggle','uia_control_select','uia_control_expand_collapse','uia_control_set_range_value'}:return 'uia_control'
    for prefix,group in [('filesystem_','filesystem'),('dom_','headless_dom'),('uia_','uia_read'),
                         ('eda_','eda_kicad'),('powershell_','windows_background'),
                         ('command_profile_','windows_background'),('worktree_','git_worktree')]:
        if name.startswith(prefix):return group
    return 'runtime_support'

def groups(runtime,catalog,version,configured,now=None):
    now=time.time() if now is None else now
    home=runtime.config_path.parent.parent
    report={}; valid=False
    try:
        versioned=home/'deployment'/('provider-verification.'+version+'.json')
        path=versioned if versioned.is_file() else home/'deployment/provider-verification.json'
        if path.stat().st_size>65536:raise ValueError('Oversized evidence')
        report=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(report,dict) or not isinstance(report.get('groups',{}),dict):raise ValueError('Invalid evidence')
        manifest=home/'runtime-versions'/version/'manifest.json'
        valid=(report.get('runtime_version')==version and
               report.get('runtime_manifest_sha256')==hashlib.sha256(manifest.read_bytes()).hexdigest() and
               0<=now-float(report['checked_at'])<=86400)
    except (OSError,ValueError,TypeError,KeyError):pass
    output=[]
    families={family(t['name']) for t in catalog['tools']}
    for name in sorted(families):
        tools=[t for t in catalog['tools'] if family(t['name'])==name]
        permitted=[t for t in tools if t['state'] not in {'missing_scope','approval_required','blocked_by_policy'}]
        probe=report.get('groups',{}).get(name,{}) if isinstance(report,dict) and isinstance(report.get('groups',{}),dict) else {}
        if not isinstance(probe,dict):probe={}
        state='configured_only' if configured.get(name,True) else 'unavailable'
        if probe and not valid:state='stale_evidence'
        if valid and probe:state='verified' if probe.get('state')=='PASS' else 'blocked' if probe.get('state')=='BLOCKED' else 'failed'
        if not configured.get(name,True):state='blocked_by_configuration'
        if not permitted:state='blocked_by_permissions'
        output.append({'name':name,'state':state,'registered_tools':len(tools),'authorized_tools':len(permitted),
                       'runtime_version':version,'checked_at':report.get('checked_at') if probe else None,
                       'evidence_source':'owner_verification_suite' if probe else 'configuration',
                       'coverage':probe.get('coverage','No live representative provider test recorded'),
                       'all_tools_verified':False,'next_action':'Run representative checks for this runtime version' if state!='verified' else 'Inspect coverage before using an untested operation'})
    return output
