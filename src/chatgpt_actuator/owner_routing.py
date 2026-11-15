"""Update routing descriptions while preserving route decisions and loop guards."""
import hashlib
import json
from pathlib import Path
from .atomic_files import atomic_write

def install(home,journal):
    path=Path(home).parent/'routing-policy.json';journal=Path(journal)
    if not path.is_file():return False
    raw=path.read_bytes();value=json.loads(raw.decode('utf-8-sig'))
    before={'actuator':value.get('actuator',{}),'execution_policy':value.get('execution_policy')}
    actuator=dict(before['actuator'])
    actuator['use_for']=['background filesystem and API operations','isolated headless DOM','owner-approved fixed command profiles','read-only desktop inspection']
    actuator['avoid_for']=['physical mouse or keyboard input','desktop UI mutation or focus','arbitrary shell or GUI launch','independent multi-worker reasoning']
    value['actuator']=actuator;value['execution_policy']='background_only'
    after={'actuator':actuator,'execution_policy':'background_only'}
    journal.write_text(json.dumps({'before':before,'after':after,'rolled_back':False},indent=2))
    atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode(),overwrite=True,expected=hashlib.sha256(raw).hexdigest())
    return True

def rollback(home,journal):
    path=Path(home).parent/'routing-policy.json';journal=Path(journal)
    if not journal.is_file():return
    record=json.loads(journal.read_text())
    if record.get('rolled_back'):return
    raw=path.read_bytes();value=json.loads(raw.decode('utf-8-sig'))
    for key,after in record['after'].items():
        if value.get(key)!=after:raise RuntimeError('Concurrent routing change; rollback stopped')
    for key,before in record['before'].items():
        if before is None:value.pop(key,None)
        else:value[key]=before
    atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode(),overwrite=True,expected=hashlib.sha256(raw).hexdigest())
    record['rolled_back']=True;journal.write_text(json.dumps(record,indent=2))
