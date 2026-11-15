"""Bounded process lineage evidence, never an automatic control grant."""
import psutil

MAX_PROCESSES=32
MAX_DEPTH=4
BROWSERS={'msedge.exe','chrome.exe','firefox.exe','brave.exe','opera.exe','iexplore.exe'}

def lineage(root,rows):
    """One process snapshot; reject parent PID reuse by creation-time ordering."""
    children={}
    for row in rows:
        if not isinstance(row.get('pid'),int) or not isinstance(row.get('started'),(int,float)):continue
        children.setdefault(row.get('parent_pid'),[]).append(row)
    family={root['pid']:{**root,'parent_pid':None,'depth':0}};queue=[root['pid']];truncated=False
    while queue:
        parent=family[queue.pop(0)]
        for child in children.get(parent['pid'],[]):
            if child['pid'] in family or child['started']<parent['started']:continue
            if parent['depth']>=MAX_DEPTH or len(family)>=MAX_PROCESSES:truncated=True;continue
            family[child['pid']]={**child,'depth':parent['depth']+1};queue.append(child['pid'])
    return family,truncated

def capture(target,include_children=True):
    root=psutil.Process(target['pid'])
    if root.create_time()!=target['started']:raise ValueError('Bound process identity changed')
    root_record={'pid':root.pid,'started':root.create_time(),'name':root.name()}
    rows=[]
    if include_children:
        # psutil uses a parent-PID snapshot and inspects creation times only
        # for descendants. Avoid opening every unrelated system process.
        for process in root.children(recursive=True):
            try:
                rows.append({'pid':process.pid,'parent_pid':process.ppid(),'started':process.create_time()})
            except psutil.Error:pass
    family,truncated=lineage(root_record,rows)
    verified={root.pid:family[root.pid]}
    for pid,row in sorted(family.items(),key=lambda pair:pair[1]['depth']):
        if pid==root.pid:continue
        try:
            process=psutil.Process(pid)
            parent=verified.get(row['parent_pid'])
            if parent and process.create_time()==row['started'] and process.ppid()==parent['pid'] and psutil.Process(parent['pid']).create_time()==parent['started']:
                verified[pid]={**row,'name':process.name()}
        except psutil.Error:pass
    if psutil.Process(root.pid).create_time()!=target['started']:raise ValueError('Bound process identity changed')
    return verified,truncated

def still_related(row,family):
    try:
        process=psutil.Process(row['pid'])
        if process.create_time()!=row['started']:return False
        if row['depth']:
            parent=family.get(row['parent_pid'])
            if not parent or process.ppid()!=parent['pid'] or not still_related(parent,family):return False
        return True
    except psutil.Error:return False
