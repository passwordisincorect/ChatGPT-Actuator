"""Each activation attempt has an immutable ID; failures never erase earlier success."""
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import time

TERMINAL={'succeeded','already_applied','failed','blocked','rolled_back'}
STATES={'preflight','applying','verifying',*TERMINAL}
CHECKS={'windows_tests','private_desktop','headless_dom','local_mcp','gateway_rpc',
        'openai_mcp','tailscale_mcp','compiler','service_start_stop'}

class UpdateHistory:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.executescript('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,target TEXT,created REAL);'
                'CREATE TABLE IF NOT EXISTS events(attempt TEXT,seq INTEGER,state TEXT,checks TEXT,created REAL,PRIMARY KEY(attempt,seq));')

    def begin(self,target):
        if not isinstance(target,str) or not 1<=len(target)<=80:raise ValueError('Invalid target version')
        ident=secrets.token_urlsafe(24)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT INTO attempts VALUES(?,?,?)',(ident,target,time.time()))
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ident,0,'preflight','{}',time.time()))
        return ident

    def record(self,ident,state,**checks):
        if state not in STATES or any(k not in CHECKS or type(v) is not bool for k,v in checks.items()):
            raise ValueError('Invalid update evidence')
        with closing(sqlite3.connect(self.path,timeout=10)) as c,c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT seq,state FROM events WHERE attempt=? ORDER BY seq DESC LIMIT 1',(ident,)).fetchone()
            if row is None or row[1] in TERMINAL:raise RuntimeError('Attempt is unknown or already terminal')
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ident,row[0]+1,state,json.dumps(checks),time.time()))

    def summary(self,active_version,*,integrity_verified=False,local_mcp_verified=False):
        with closing(sqlite3.connect(self.path)) as c:
            rows=c.execute('SELECT a.id,a.target,a.created,e.state,e.checks,e.created FROM attempts a JOIN events e ON e.attempt=a.id WHERE e.seq=(SELECT MAX(x.seq) FROM events x WHERE x.attempt=a.id) ORDER BY a.created DESC LIMIT 50').fetchall()
        merged={}
        with closing(sqlite3.connect(self.path)) as c:
            for ident,checks in c.execute('SELECT attempt,checks FROM events ORDER BY attempt,seq'):
                merged.setdefault(ident,{}).update(json.loads(checks))
        attempts=[{'attempt_id':i,'target_version':v,'started_at':start,'state':s,'checks':merged.get(i,{}),'checked_at':end} for i,v,start,s,ch,end in rows]
        return {'active_version':active_version,
            'active_state':'verified' if integrity_verified and local_mcp_verified else 'selected_unverified',
            'attempts':attempts,'note':'A failed attempt does not change the independently verified active runtime state'}

def apply_once(active,target,verify,apply):
    if active==target:
        verify()
        return 'already_applied'
    apply()
    return 'succeeded'
