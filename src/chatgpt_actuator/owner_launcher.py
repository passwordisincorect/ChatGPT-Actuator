"""Owner-only launch coordinator. Process liveness is separate from remote MCP proof."""
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import time
import traceback

STEPS=('integrity','supervisor','local_services','control_center','openai_start','local_mcp','gateway_mcp','owned_tunnel','resume_study')
ACTIONS={'start','restart','restore','status'}
class LaunchJournal:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.executescript('CREATE TABLE IF NOT EXISTS launch_attempts(id TEXT PRIMARY KEY,version TEXT,action TEXT,parent TEXT,created REAL);CREATE TABLE IF NOT EXISTS launch_events(attempt TEXT,seq INTEGER,step TEXT,state TEXT,error TEXT,created REAL,PRIMARY KEY(attempt,seq));')
    def begin(self,version,action,parent=None):
        if action not in ACTIONS:raise ValueError('Unknown owner action')
        ident=secrets.token_urlsafe(24)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT INTO launch_attempts VALUES(?,?,?,?,?)',(ident,version,action,parent,time.time()))
        return ident
    def event(self,ident,step,state,error=None):
        if step not in {*STEPS,'attempt','stop'} or state not in {'running','succeeded','failed','skipped','busy'}:raise ValueError('Invalid launch event')
        safe=None
        if error is not None:
            safe={'type':type(error).__name__,'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)]}
        with closing(sqlite3.connect(self.path,timeout=10)) as c,c:
            c.execute('BEGIN IMMEDIATE')
            seq=c.execute('SELECT COALESCE(MAX(seq),-1)+1 FROM launch_events WHERE attempt=?',(ident,)).fetchone()[0]
            c.execute('INSERT INTO launch_events VALUES(?,?,?,?,?,?)',(ident,seq,step,state,json.dumps(safe) if safe else None,time.time()))
    def latest(self,limit=10,*,attempt_id=None):
        with closing(sqlite3.connect(self.path)) as c:
            attempts=c.execute('SELECT id,version,action,parent,created FROM launch_attempts WHERE (? IS NULL OR id=?) ORDER BY created DESC LIMIT ?',(attempt_id,attempt_id,limit)).fetchall()
            result=[]
            for ident,version,action,parent,created in attempts:
                events=c.execute('SELECT step,state,error,created FROM launch_events WHERE attempt=? ORDER BY seq',(ident,)).fetchall()
                first=next(({'step':s,'error':json.loads(e)} for s,state,e,_ in events if state=='failed' and e),None)
                steps={s:{'state':state,'checked_at':at} for s,state,_,at in events if s!='attempt'}
                state=next((state for s,state,_,_ in reversed(events) if s=='attempt'),'running')
                result.append({'attempt_id':ident,'version':version,'action':action,'parent_attempt':parent,'state':state,'first_failure':first,'steps':steps,'started_at':created})
            return result

def run_launcher(home,action,runner=None,*,existing_lease=None,parent_attempt=None):
    if action not in ACTIONS:raise ValueError('Unknown owner action')
    home=Path(home)
    version=json.loads((home/'runtime-current.json').read_text())['version']
    journal=LaunchJournal(home/'deployment/launch-history.sqlite3');ident=journal.begin(version,action,parent_attempt)
    lease=None;sessions=None;resume=False
    try:
        if runner is None:
            from .owner_native_runner import NativeRunner
            runner=NativeRunner(home,version)
        if action!='status':
            from .work_sessions import WorkSessions
            sessions=WorkSessions(home/'config/work-sessions.sqlite3')
            if existing_lease:
                with sessions.db() as c:row=c.execute('SELECT exclusive,pid,started FROM operations WHERE id=?',(existing_lease,)).fetchone()
                if row!=(1,sessions.pid,sessions.started):raise PermissionError('Invalid owner update lease')
            else:lease=sessions.acquire_operation(None,'node.owner_launcher',exclusive=True)
        journal.event(ident,'attempt','running')
        def step(name,fn):
            journal.event(ident,name,'running')
            try:fn()
            except BaseException as exc:journal.event(ident,name,'failed',exc);raise
            journal.event(ident,name,'succeeded')
        step('integrity',runner.integrity)
        if action=='restart':
            journal.event(ident,'stop','running')
            try:resume=runner.stop()
            except BaseException as exc:journal.event(ident,'stop','failed',exc);raise
            journal.event(ident,'stop','succeeded')
        if action!='status':
            step('supervisor',runner.supervisor)
            step('local_services',runner.local_services)
            step('control_center',runner.control_center)
            step('openai_start',runner.openai_start)
        step('local_mcp',runner.local_mcp)
        step('gateway_mcp',runner.gateway_mcp)
        step('owned_tunnel',runner.owned_tunnel)
        if resume:step('resume_study',runner.resume_study)
        journal.event(ident,'attempt','succeeded')
        return {'attempt_id':ident,'state':'local_ready','version':version,'remote_mcp':'awaiting_actual_connector_calls'}
    except Exception as exc:
        if not journal.latest(attempt_id=ident)[0]['first_failure']:
            journal.event(ident,'attempt','failed',exc)
        journal.event(ident,'attempt','failed')
        return {'attempt_id':ident,'state':'failed','version':version,'first_failure':journal.latest(attempt_id=ident)[0]['first_failure']}
    finally:
        if lease:sessions.release_operation(lease)
