"""Bounded staged UTF-8 text versions, verified publication and guarded restoration."""
import difflib
import gzip
import hashlib
import io
import json
import time
from pathlib import Path

from ..missions.store import encode

MAX_TEXT=16384
MAX_RAW=65536


def initialize(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS agent_artifacts(
            id INTEGER PRIMARY KEY,job TEXT,owner TEXT,path TEXT,cycle INTEGER,mission TEXT,
            state TEXT,created REAL,before_sha TEXT,after_sha TEXT,payload BLOB,raw_size INTEGER,
            UNIQUE(job,cycle))''')


def unpack(payload,raw_size):
    if payload is None:
        raise ValueError('Artifact details expired')
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        raw=stream.read(MAX_RAW+1)
    if len(raw)>MAX_RAW or len(raw)!=raw_size:
        raise RuntimeError('Artifact history size mismatch')
    return json.loads(raw)


async def stage(service,doc,principal,action,after):
    initialize(service.queue.store)
    if action['tool']!='filesystem_write_text':
        raise ValueError('Versioned output supports native UTF-8 text writes only')
    args=action['arguments'];content=args['content'];path=str(Path(args['path']).resolve())
    if len(content.encode('utf-8'))>MAX_TEXT:
        raise ValueError('Versioned text exceeds 16 KiB')
    with service.queue.store.db() as db:
        old=db.execute('SELECT id,path,payload,raw_size FROM agent_artifacts WHERE job=? AND cycle=?',(doc['id'],doc['cycles'])).fetchone()
    if old:
        if old[1]!=path or unpack(old[2],old[3])['after']!=content:
            raise ValueError('Staged cycle conflicts with another proposal')
        return old[0]
    before,before_sha=None,None
    read={'tool':'filesystem_read_text','arguments':{'path':path}}
    service._check_action(doc,read,principal)
    try:
        value=await service.engine._call(read,principal,time.monotonic()+10)
        before,before_sha=value['content'],value['sha256']
        if value.get('truncated') or len(before.encode('utf-8'))>MAX_TEXT or hashlib.sha256(before.encode('utf-8')).hexdigest()!=before_sha:
            raise ValueError('Versioning requires complete canonical UTF-8 text up to 16 KiB')
        if args.get('expected_sha256')!=before_sha or not args.get('overwrite'):
            raise ValueError('Staging requires the same native CAS baseline as the planned write')
    except FileNotFoundError:
        if args.get('overwrite') or args.get('expected_sha256') is not None:
            raise ValueError('Staged create conflicts with an existing-file proposal')
    from .quality import evaluate
    from .graphs import current
    node=current(doc)
    specs=doc.get('validators',[])+(node['recipe']['validators'] if node and node['recipe'] else [])
    for spec in specs:
        if Path(spec['path']).resolve()==Path(path) and not evaluate(spec,content)['passed']:
            raise ValueError('Staged output fails its frozen quality contract')
    for condition in after:
        if condition['tool']=='filesystem_read_text' and Path(condition['arguments'].get('path','')).resolve()==Path(path) and condition['path']==['content']:
            if condition['op']=='eq' and content!=condition['value'] or condition['op']=='contains' and condition['value'] not in content:
                raise ValueError('Staged content fails its declared postcondition')
    raw=encode({'before':before,'after':content}).encode('utf-8');payload=gzip.compress(raw,mtime=0)
    if len(raw)>MAX_RAW:
        raise ValueError('Artifact history exceeds bound')
    service.queue.assert_claim(doc)
    with service.queue.store.db() as db:
        current=service.queue.get(db,doc['owner'],doc['id'])
        if current.get('_claim_token')!=doc.get('_claim_token') or current['state']!='RUNNING':
            raise ValueError('Task changed while staging')
        db.execute('UPDATE agent_artifacts SET payload=NULL WHERE created<?',(time.time()-90*86400,))
        total=db.execute('SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM agent_artifacts').fetchone()
        own=db.execute('SELECT COUNT(*) FROM agent_artifacts WHERE owner=?',(doc['owner'],)).fetchone()[0]
        if total[0]>=1000 or own>=200 or total[1]+len(payload)>16*1024*1024:
            raise RuntimeError('Retained artifact history limit reached')
        cursor=db.execute('INSERT INTO agent_artifacts(job,owner,path,cycle,state,created,before_sha,after_sha,payload,raw_size) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (doc['id'],doc['owner'],path,doc['cycles'],'STAGED',time.time(),before_sha,hashlib.sha256(content.encode('utf-8')).hexdigest(),payload,len(raw)))
        return cursor.lastrowid


def bind(store,version_id,mission):
    with store.db() as db:
        db.execute('UPDATE agent_artifacts SET mission=? WHERE id=? AND (mission IS NULL OR mission=?)',(mission,version_id,mission))


async def commit(service,doc,principal,mission):
    initialize(service.queue.store)
    with service.queue.store.db() as db:
        rows=db.execute('SELECT id,path,after_sha FROM agent_artifacts WHERE job=? AND mission=?',(doc['id'],mission)).fetchall()
    for version_id,path,expected in rows:
        action={'tool':'filesystem_read_text','arguments':{'path':path}}
        service._check_action(doc,action,principal)
        observed=await service.engine._call(action,principal,time.monotonic()+10)
        if observed.get('sha256')!=expected or observed.get('truncated'):
            raise RuntimeError('Published output changed before version verification')
        with service.queue.store.db() as db:
            db.execute("UPDATE agent_artifacts SET state='VERIFIED' WHERE id=?",(version_id,))


def history(queue,owner,job_id):
    initialize(queue.store)
    with queue.store.db() as db:
        queue.get(db,owner,job_id)
        rows=db.execute('SELECT id,path,state,created,before_sha,after_sha,payload,raw_size FROM agent_artifacts WHERE job=? AND owner=? ORDER BY id DESC LIMIT 50',(job_id,owner)).fetchall()
    items=[]
    for version_id,path,state,created,before_sha,after_sha,payload,raw_size in rows:
        item={'id':version_id,'path':path,'state':state,'created':created,'before_sha256':before_sha,'after_sha256':after_sha,'details_available':payload is not None}
        if payload is not None:
            bundle=unpack(payload,raw_size);lines=[];size=0;truncated=False
            for line in difflib.unified_diff((bundle['before'] or '').splitlines(True),bundle['after'].splitlines(True),fromfile='before',tofile='after',n=3):
                if len(lines)>=120 or size+len(line.encode('utf-8'))>16384:
                    truncated=True;break
                lines.append(line);size+=len(line.encode('utf-8'))
            item.update(diff=''.join(lines),diff_truncated=truncated,can_restore_text=state=='VERIFIED' and bundle['before'] is not None)
        items.append(item)
    return {'job_id':job_id,'versions':items}


def restore(service,principal,job_id,version_id,request_key):
    initialize(service.queue.store)
    if type(version_id) is not int:
        raise ValueError('Numeric artifact version required')
    with service.queue.store.db() as db:
        doc=service.queue.get(db,principal.owner,job_id)
        row=db.execute('SELECT path,state,after_sha,payload,raw_size FROM agent_artifacts WHERE id=? AND job=? AND owner=?',(version_id,job_id,principal.owner)).fetchone()
        if doc.get('archived'):
            doc=service.queue.archive.read(db,principal.owner,job_id)['job']
    if not row or row[1]!='VERIFIED' or doc.get('workspace') is None:
        raise ValueError('Verified retained artifact delegation required')
    bundle=unpack(row[3],row[4])
    if bundle['before'] is None:
        raise ValueError('Restoring a previously absent file would require deletion; unsupported')
    from .recipes import prepare
    recipe,criteria=prepare(doc['workspace'],{'kind':'files','outputs':[{'path':row[0],'text':bundle['before']}]})
    recipe['required_sha256']={row[0]:row[2]}
    from dataclasses import replace
    delegated=replace(principal,scopes=principal.scopes & frozenset(doc['delegated_scopes']),full_access=False)
    return service.submit(delegated,'Restore previous text for artifact '+str(version_id),doc['workspace'],
        ['filesystem_read_text','filesystem_write_text'],criteria,request_key,recipe=recipe,version_outputs=True)
