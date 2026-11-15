"""Owner-local, field-level configuration changes with guarded reversal."""
import hashlib
import json
from pathlib import Path
from .atomic_files import atomic_write
from .profile_defaults import discover
from .resource_limits import validated

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def install(home,package,journal):
    home=Path(home);path=home/'config/config.json';journal=Path(journal)
    raw=path.read_bytes();cfg=json.loads(raw.decode('utf-8-sig'));security=cfg.setdefault('security',{})
    profiles=security.setdefault('command_profiles',{})
    if not isinstance(profiles,dict):raise ValueError('Existing profiles are invalid')
    old_limits=security.get('resource_limits');limits=validated(old_limits)
    additions={name:value for name,value in discover(home,package).items() if name not in profiles}
    record={'added_profiles':{name:digest(value) for name,value in additions.items()},
            'old_limits':old_limits,'new_limits':limits,'rolled_back':False}
    if journal.exists():
        previous=json.loads(journal.read_text())
        if not previous.get('rolled_back'):raise RuntimeError('Configuration update journal already active')
    journal.write_text(json.dumps(record,indent=2))
    profiles.update(additions);security['resource_limits']=limits
    workspace=home.parent/'Actuator-Workspace';workspace.mkdir(exist_ok=True);(workspace/'empty-hooks').mkdir(exist_ok=True)
    atomic_write(path,json.dumps(cfg,ensure_ascii=False,indent=2).encode(),overwrite=True,expected=hashlib.sha256(raw).hexdigest())
    return sorted(additions)
def rollback(home,journal):
    path=Path(home)/'config/config.json';journal=Path(journal)
    if not journal.is_file():return
    record=json.loads(journal.read_text())
    if record.get('rolled_back'):return
    raw=path.read_bytes();cfg=json.loads(raw.decode('utf-8-sig'));security=cfg.get('security',{});profiles=security.get('command_profiles',{})
    if security.get('resource_limits')!=record['new_limits']:raise RuntimeError('Concurrent resource configuration change; rollback stopped')
    for name,expected in record['added_profiles'].items():
        if name in profiles and digest(profiles[name])!=expected:raise RuntimeError('Concurrent command profile change; rollback stopped')
    for name in record['added_profiles']:profiles.pop(name,None)
    if record['old_limits'] is None:security.pop('resource_limits',None)
    else:security['resource_limits']=record['old_limits']
    atomic_write(path,json.dumps(cfg,ensure_ascii=False,indent=2).encode(),overwrite=True,expected=hashlib.sha256(raw).hexdigest())
    record['rolled_back']=True;journal.write_text(json.dumps(record,indent=2))
