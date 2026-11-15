"""Read-only post-activation observations; never restart services or infer remote OAuth proof."""
from contextlib import closing
import json,sqlite3,time
from pathlib import Path
CHECKPOINTS=(30,120,600)
SCHEDULE_GRACE_SECONDS=30
class HealthHistory:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.executescript('CREATE TABLE IF NOT EXISTS health_batches(id TEXT PRIMARY KEY,version TEXT,started REAL);CREATE TABLE IF NOT EXISTS health_observations(batch TEXT,delay INTEGER,state TEXT,details TEXT,checked REAL,PRIMARY KEY(batch,delay));')
    def begin(self,ident,version,started=None):
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT OR IGNORE INTO health_batches VALUES(?,?,?)',(ident,version,time.time() if started is None else started))
            row=c.execute('SELECT version,started FROM health_batches WHERE id=?',(ident,)).fetchone()
            if row[0]!=version:raise ValueError('Observation version mismatch')
            return row[1]
    def record(self,ident,delay,state,details=None):
        if state not in {'succeeded','failed','superseded'} or delay not in CHECKPOINTS:raise ValueError('Invalid health observation')
        # Only structured step/type, never arbitrary exception text or command output.
        safe={k:v for k,v in (details or {}).items() if k in {'step','error_type'}}
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT OR IGNORE INTO health_observations VALUES(?,?,?,?,?)',(ident,delay,state,json.dumps(safe),time.time()))
    def summary(self,version,*,attempt_id=None):
        with closing(sqlite3.connect(self.path)) as c:
            row=c.execute('SELECT id,started FROM health_batches WHERE version=? AND (? IS NULL OR id=?) ORDER BY rowid DESC LIMIT 1',(version,attempt_id,attempt_id)).fetchone()
            if row is None:return {'state':'not_scheduled','checkpoints':[]}
            seen={d:{'delay_seconds':d,'state':s,'details':json.loads(v),'checked_at':t} for d,s,v,t in c.execute('SELECT delay,state,details,checked FROM health_observations WHERE batch=?',(row[0],))}
        checks=[seen.get(d,{'delay_seconds':d,'state':'pending'}) for d in CHECKPOINTS]
        state='failed' if any(x['state']=='failed' for x in checks) else 'superseded' if any(x['state']=='superseded' for x in checks) else 'verified' if all(x['state']=='succeeded' for x in checks) else 'observing'
        return {'attempt_id':row[0],'started_at':row[1],'state':state,'checkpoints':checks,'read_only':True}
def schedule(home,version,ident,*,scheduler=None):
    """Persist the activation time before dispatching the existing owner task."""
    home=Path(home)
    history=HealthHistory(home/'deployment/post-update-health.sqlite3')
    history.begin(ident,version)
    if scheduler is None:
        from .owner_native_runner import NativeRunner,powershell_file
        runner=NativeRunner(home,version)
        scheduler=lambda:powershell_file(runner.ps,home/'scripts/schedule-health-checks.ps1',home,
            runner.env,30,('-Version',version,'-Attempt',ident))
    scheduler()
    return history.summary(version)

def observe(home,version,ident,*,probe=None,wait=time.sleep,clock=time.monotonic,wall=time.time):
    home=Path(home);history=HealthHistory(home/'deployment/post-update-health.sqlite3')
    started=history.begin(ident,version,started=wall())
    if probe is None:
        from .owner_launcher import run_launcher
        probe=lambda:run_launcher(home,'status')
    # A restarted observer resumes the original batch, not a new ten-minute window.
    began=clock()-max(0,wall()-started)
    with closing(sqlite3.connect(history.path)) as db:
        seen={row[0] for row in db.execute('SELECT delay FROM health_observations WHERE batch=?',(ident,))}
    for delay in CHECKPOINTS:
        if delay in seen:continue
        if history.summary(version).get('attempt_id')!=ident:
            for pending in CHECKPOINTS:
                if pending not in seen:history.record(ident,pending,'superseded')
            return history.summary(version,attempt_id=ident)
        remaining=delay-(clock()-began)
        if remaining>0:wait(remaining)
        if json.loads((home/'runtime-current.json').read_text())['version']!=version:
            for pending in CHECKPOINTS:
                if pending not in seen:history.record(ident,pending,'superseded')
            return history.summary(version,attempt_id=ident)
        if clock()-began>delay+SCHEDULE_GRACE_SECONDS:
            history.record(ident,delay,'failed',{'step':'observation_schedule','error_type':'ObservationMissed'})
            continue
        try:
            result=probe()
            if result['state']=='local_ready':history.record(ident,delay,'succeeded')
            else:
                failure=result.get('first_failure') or {};error=failure.get('error') or {}
                history.record(ident,delay,'failed',{'step':failure.get('step'),'error_type':error.get('type')})
        except Exception as exc:history.record(ident,delay,'failed',{'error_type':type(exc).__name__})
    return history.summary(version,attempt_id=ident)
