"""Field-level owner installation/rollback, never copies the complete owner config."""
import hashlib
import json
from pathlib import Path
from .tunnel_credentials import atomic
from .profile_preflight import inspect_profile

def install(home,source,journal):
    home=Path(home);source=Path(source);journal=Path(journal)
    path=home/'config/config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
    profiles=cfg['security']['command_profiles'];runner=home/'scripts/service-profile.py'
    additions={}
    for action in ('start','status','stop'):
        name='study-coach-'+action
        profile={'executable':str(home/'.venv/Scripts/python.exe'),'arguments':['-I','-B',str(runner),action],
            'executable_sha256':hashlib.sha256((home/'.venv/Scripts/python.exe').read_bytes()).hexdigest(),
            'cwd':str(home.parent/'Actuator-Workspace'),'background_approved':True,
            'timeout_seconds':40,'max_output_bytes':16384,
            'input_sha256':{str(runner):hashlib.sha256((source/'scripts/service-profile.py').read_bytes()).hexdigest()}}
        if name in profiles:raise RuntimeError('Owner service profile already exists')
        if not inspect_profile(profile,cfg['filesystem']['allowed_roots'],cfg['security']['resource_limits'])['configuration_ready']:
            raise RuntimeError('Service profile preflight failed')
        additions[name]=profile
    record={'added':{name:hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest() for name,value in additions.items()},'rolled_back':False}
    if journal.exists() and not json.loads(journal.read_text()).get('rolled_back'):
        raise RuntimeError('Configuration journal is already active')
    journal.write_text(json.dumps(record,indent=2))
    profiles.update(additions)
    try:atomic(path,json.dumps(cfg,ensure_ascii=False,indent=2).encode(),before)
    except BaseException:
        record['rolled_back']=True;journal.write_text(json.dumps(record,indent=2));raise
    return sorted(additions)

def rollback(home,journal):
    journal=Path(journal)
    if not journal.is_file():return
    record=json.loads(journal.read_text())
    if record.get('rolled_back'):return
    path=Path(home)/'config/config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
    profiles=cfg['security']['command_profiles']
    for name,expected in record['added'].items():
        if name in profiles and hashlib.sha256(json.dumps(profiles[name],sort_keys=True).encode()).hexdigest()!=expected:
            raise RuntimeError('Owner service profile changed; rollback stopped')
    for name in record['added']:profiles.pop(name,None)
    atomic(path,json.dumps(cfg,ensure_ascii=False,indent=2).encode(),before)
    record['rolled_back']=True;journal.write_text(json.dumps(record,indent=2))
