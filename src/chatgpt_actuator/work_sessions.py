"""Persistent resource ownership. Closed/interrupted sessions cannot reuse handles."""
from contextlib import contextmanager
import json
import os
import psutil
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from .tool_policy import principal_context

class WorkSessions:
    def __init__(self,path,limits=None):
        from .resource_limits import validated
        self.limits=validated(limits)
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.defaults={}
        self.runtime_id=secrets.token_urlsafe(24)
        self.pid=os.getpid()
        # /proc may expose host PIDs while getpid() uses a nested PID namespace.
        if os.name == 'posix':
            try:
                exposed=os.readlink('/proc/self')
                if exposed.isdigit(): self.pid=int(exposed)
            except OSError: pass
        self.started=psutil.Process(self.pid).create_time()
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS runtimes(id TEXT PRIMARY KEY,pid INTEGER,started REAL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,owner TEXT,status TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session TEXT,tool TEXT,status TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS handles(id TEXT PRIMARY KEY,session TEXT,kind TEXT,value TEXT,
                UNIQUE(kind,value));
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,session TEXT,tool TEXT,
                exclusive INTEGER,pid INTEGER,started REAL,created REAL);
            CREATE TABLE IF NOT EXISTS retired_refs(id TEXT PRIMARY KEY,session TEXT,reason TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,session TEXT,key TEXT,
                fingerprint TEXT,status TEXT,cancel INTEGER,result TEXT,runtime_id TEXT,created REAL,
                UNIQUE(session,key));
            """)
            # Serialize migration and recovery across independently running backends.
            c.execute("BEGIN IMMEDIATE")
            c.execute('CREATE TABLE IF NOT EXISTS attempt_labels(id TEXT PRIMARY KEY,label TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS attempt_lifecycle(id TEXT PRIMARY KEY,started REAL,finished REAL)')
            columns={row[1] for row in c.execute("PRAGMA table_info(sessions)")}
            if "runtime_id" not in columns:
                c.execute("ALTER TABLE sessions ADD COLUMN runtime_id TEXT")
            if "last_seen" not in columns:
                c.execute("ALTER TABLE sessions ADD COLUMN last_seen REAL")
                c.execute("UPDATE sessions SET last_seen=created")
            # Legacy rows lack process ownership and cannot safely be resumed.
            c.execute("UPDATE jobs SET status='interrupted' WHERE status='running' AND session IN "
                      "(SELECT id FROM sessions WHERE runtime_id IS NULL)")
            c.execute("UPDATE sessions SET status='interrupted' WHERE status='active' AND runtime_id IS NULL")
            for rid,pid,started in c.execute("SELECT id,pid,started FROM runtimes").fetchall():
                if self._runtime_alive(pid,started):
                    continue
                c.execute("UPDATE jobs SET status='interrupted' WHERE status='running' AND session IN "
                          "(SELECT id FROM sessions WHERE runtime_id=?)",(rid,))
                c.execute("UPDATE sessions SET status='interrupted' WHERE status='active' AND runtime_id=?",(rid,))
                c.execute("UPDATE attempts SET status='interrupted' WHERE status='running' AND runtime_id=?",(rid,))
                c.execute("DELETE FROM runtimes WHERE id=?",(rid,))
            c.execute("INSERT INTO runtimes VALUES(?,?,?)",(self.runtime_id,self.pid,self.started))

    @staticmethod
    def _runtime_alive(pid,started):
        try:
            return psutil.Process(pid).create_time()==started
        except (psutil.NoSuchProcess,psutil.ZombieProcess):
            return False
        except (psutil.AccessDenied,OSError):
            # Uncertain liveness must not invalidate a running owner's resources.
            return True

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=10)
        try:
            with connection: yield connection
        finally:
            connection.close()
    def open(self,owner):
        self.maintenance()
        sid=secrets.token_urlsafe(24)
        with self.lock,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0]>=self.limits['max_sessions_total']:
                raise PermissionError('RESOURCE_LIMIT: active session budget reached')
            if c.execute("SELECT COUNT(*) FROM sessions WHERE owner=? AND status='active'",(owner,)).fetchone()[0]>=self.limits['max_sessions_per_owner']:
                raise PermissionError('RESOURCE_LIMIT: owner session budget reached')
            now=time.time()
            c.execute("INSERT INTO sessions(id,owner,status,created,runtime_id,last_seen) VALUES(?,?,?,?,?,?)",
                      (sid,owner,"active",now,self.runtime_id,now))
        return {"session_id":sid,"status":"active"}
    def require(self,principal):
        self.maintenance()
        if principal.session=="default":
            with self.lock:
                if principal.owner in self.defaults:
                    with self.db() as c:
                        current=c.execute('SELECT status FROM sessions WHERE id=?',(self.defaults[principal.owner],)).fetchone()
                    if current!=('active',):self.defaults.pop(principal.owner,None)
                if principal.owner not in self.defaults:
                    self.defaults[principal.owner]=self.open(principal.owner)["session_id"]
                sid=self.defaults[principal.owner]
            with self.db() as c:
                row=c.execute("SELECT status FROM sessions WHERE id=?",(sid,)).fetchone()
                c.execute('UPDATE sessions SET last_seen=? WHERE id=?',(time.time(),sid))
            if row[0]!="active": raise PermissionError("Implicit session is closed")
            return sid
        with self.db() as c:
            row=c.execute("SELECT owner,status FROM sessions WHERE id=?",(principal.session,)).fetchone()
        if not row or row!=(principal.owner,"active"):
            from .automation_errors import PolicyDenied
            raise PolicyDenied('wrong_owner')
        with self.db() as c:c.execute('UPDATE sessions SET last_seen=? WHERE id=?',(time.time(),principal.session))
        return principal.session

    def maintenance(self,now=None,idle_seconds=1800,retention_seconds=2592000):
        """Expire only resource-free idle sessions; retain terminal history for 30 days."""
        now=time.time() if now is None else now
        with self.lock,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            self._recover_operations(c)
            c.execute('DELETE FROM retired_refs WHERE created<?',(now-3600,))
            safe="NOT EXISTS(SELECT 1 FROM handles h WHERE h.session=s.id AND h.kind!='dom_closed_tab_ref') AND NOT EXISTS(SELECT 1 FROM operations o WHERE o.session=s.id) AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.session=s.id AND j.status='running') AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.session=s.id AND a.status='running')"
            idle=c.execute("SELECT s.id FROM sessions s WHERE s.status='active' AND s.last_seen<? AND "+safe,(now-idle_seconds,)).fetchall()
            for (sid,) in idle:
                c.execute("UPDATE sessions SET status='expired' WHERE id=?",(sid,))
                c.execute('DELETE FROM handles WHERE session=?',(sid,))
            old=c.execute("SELECT s.id FROM sessions s WHERE s.status!='active' AND s.last_seen<? AND "+safe,(now-retention_seconds,)).fetchall()
            for (sid,) in old:
                c.execute('DELETE FROM handles WHERE session=?',(sid,))
                c.execute('DELETE FROM attempt_labels WHERE id IN (SELECT id FROM attempts WHERE session=?)',(sid,))
                c.execute('DELETE FROM attempt_lifecycle WHERE id IN (SELECT id FROM attempts WHERE session=?)',(sid,))
                c.execute('DELETE FROM attempts WHERE session=?',(sid,))
                c.execute('DELETE FROM jobs WHERE session=?',(sid,))
                c.execute('DELETE FROM sessions WHERE id=?',(sid,))
        return {'expired':len(idle),'history_sessions_removed':len(old)}
    def close(self,owner,sid):
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT owner,status FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row or row[0]!=owner: raise PermissionError("Session owner mismatch")
            if row[1] != "active": raise PermissionError("Session is not active")
            blockers=self._close_blockers(c,sid)
            if blockers: return {"session_id":sid,"status":"active","closed":False,"blockers":blockers}
            c.execute("UPDATE sessions SET status='closed' WHERE id=?",(sid,))
            c.execute("DELETE FROM handles WHERE session=?",(sid,))
            c.execute('DELETE FROM retired_refs WHERE session=?',(sid,))
            if self.defaults.get(owner)==sid: self.defaults.pop(owner,None)
        return {"session_id":sid,"status":"closed","closed":True,"resources_auto_terminated":False}

    def _close_blockers(self,c,sid):
        self._recover_operations(c)
        blockers=[]
        count=c.execute("SELECT COUNT(*) FROM jobs WHERE session=? AND status='running'",(sid,)).fetchone()[0]
        if count: blockers.append({"kind":"running_jobs","count":count})
        count=c.execute("SELECT COUNT(*) FROM attempts WHERE session=? AND status='running'",(sid,)).fetchone()[0]
        if count: blockers.append({"kind":"unfinished_attempts","count":count})
        count=c.execute("SELECT COUNT(*) FROM operations WHERE session=? AND tool NOT IN "
            "('work_session_close','work_session_close_preview')",(sid,)).fetchone()[0]
        if count: blockers.append({"kind":"active_calls","count":count})
        for handle,kind,value in c.execute("SELECT id,kind,value FROM handles WHERE session=?",(sid,)):
            value=json.loads(value)
            if kind=="pid" and self._runtime_alive(*value): blockers.append({"kind":"live_process","pid":value[0]})
            elif kind=="dom_tab_ref": blockers.append({"kind":"open_browser_tab","tab_ref":handle,"next_action":"Close this owned tab with dom_close_tab"})
            elif kind=="worktree":
                from .worktrees import dirty_status
                state=dirty_status(Path(value))
                if state != "clean": blockers.append({"kind":"worktree","state":state,"path":value})
            elif kind=="test_run": blockers.append({"kind":"test_run","run_id":value,"next_action":"Stop the run and finish its cleanup"})
        return blockers

    def close_preview(self,owner,sid):
        with self.lock,self.db() as c:
            row=c.execute("SELECT owner,status FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row or row[0]!=owner: raise PermissionError("Session owner mismatch")
            blockers=self._close_blockers(c,sid)
        return {"session_id":sid,"status":row[1],"can_close":row[1]=="active" and not blockers,"blockers":blockers}

    def status(self,owner,limit=50,session_offset=0,job_offset=0):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1..100')
        if any(type(v) is not int or not 0<=v<=10000000 for v in (session_offset,job_offset)):raise ValueError('Invalid history offset')
        with self.db() as c:
            rows=c.execute("SELECT id,status,created FROM sessions WHERE owner=? ORDER BY (status='active') DESC,created DESC,id DESC LIMIT ? OFFSET ?",(owner,limit+1,session_offset)).fetchall()
            jobs=c.execute("SELECT j.id,j.session,j.tool,j.status,j.created FROM jobs j JOIN sessions s ON s.id=j.session WHERE s.owner=? ORDER BY (j.status='running') DESC,j.created DESC,j.id DESC LIMIT ? OFFSET ?",(owner,limit+1,job_offset)).fetchall()
        paging={'limit':limit,'session_offset':session_offset,'job_offset':job_offset,'next_session_offset':session_offset+limit if len(rows)>limit else None,'next_job_offset':job_offset+limit if len(jobs)>limit else None,'consistency':'current_view_may_change_between_pages'}
        return {"sessions":[{"session_id":s,"status":st,"created":t} for s,st,t in rows[:limit]],
                "jobs":[{"id":i,"session_id":s,"tool":t,"status":st,"created":ts} for i,s,t,st,ts in jobs[:limit]],'pagination':paging}
    def bind(self,sid,kind,value):
        data=json.dumps(value,sort_keys=True)
        with self.lock,self.db() as c:
            row=c.execute("SELECT id,session FROM handles WHERE kind=? AND value=?",(kind,data)).fetchone()
            if row:
                if row[1]!=sid:
                    state=c.execute("SELECT status FROM sessions WHERE id=?",(row[1],)).fetchone()
                    if state and state[0]=="active":
                        from .automation_errors import PolicyDenied
                        raise PolicyDenied('wrong_owner')
                    c.execute("DELETE FROM handles WHERE id=?",(row[0],))
                else:
                    return row[0]
            handle="ws_"+secrets.token_urlsafe(24)
            c.execute("INSERT INTO handles VALUES(?,?,?,?)",(handle,sid,kind,data))
            return handle
    def resolve(self,sid,kind,handle):
        with self.db() as c:
            row=c.execute("SELECT session,kind,value FROM handles WHERE id=?",(handle,)).fetchone()
            if not row:
                retired=c.execute('SELECT reason FROM retired_refs WHERE id=? AND session=? AND created>?',(handle,sid,time.time()-3600)).fetchone()
                if retired:
                    from .automation_errors import ReferenceUnavailable
                    raise ReferenceUnavailable(retired[0])
            if row and row[0]==sid and row[1]=='dom_closed_tab_ref' and kind=='dom_tab_ref':
                from .automation_errors import ReferenceUnavailable
                raise ReferenceUnavailable('tab_closed')
        if not row or row[0]!=sid or row[1]!=kind:
            from .automation_errors import PolicyDenied
            raise PolicyDenied('wrong_owner')
        return json.loads(row[2])
    def remember_retired(self,db,sid,handle,reason):
        db.execute('INSERT OR REPLACE INTO retired_refs VALUES(?,?,?,?)',(handle,sid,reason,time.time()))
        db.execute('DELETE FROM retired_refs WHERE created<?',(time.time()-3600,))
        db.execute('DELETE FROM retired_refs WHERE session=? AND id NOT IN (SELECT id FROM retired_refs WHERE session=? ORDER BY created DESC LIMIT 1000)',(sid,sid))
    def require_pid(self,sid,pid):
        import psutil
        value=[int(pid),psutil.Process(int(pid)).create_time()]
        data=json.dumps(value,sort_keys=True)
        with self.db() as c:
            row=c.execute("SELECT session FROM handles WHERE kind='pid' AND value=?",(data,)).fetchone()
        if not row or row[0]!=sid: raise PermissionError("Process not owned by this session")
    def release_uia_refs(self,sid,workspace):
        """Forget refs from the closed private workspace, keeping other workspaces."""
        with self.lock,self.db() as db:
            rows=db.execute('SELECT id,kind,value FROM handles WHERE session=?',(sid,)).fetchall()
            windows={json.loads(value)['hwnd'] for _,kind,value in rows if kind=='uia_control_target' and json.loads(value).get('workspace')==workspace}
            for handle,kind,value in rows:
                raw=json.loads(value);remove=(kind=='uia_control_target' and raw.get('workspace')==workspace) or (kind=='uia_workspace' and raw.get('key')==workspace)
                if kind=='hwnd':remove=raw in windows
                if kind=='uia_element_ref':
                    try:remove=int(raw.split(':')[1]) in windows
                    except (ValueError,IndexError,AttributeError):remove=False
                if remove:
                    self.remember_retired(db,sid,handle,'workspace_closed');db.execute('DELETE FROM handles WHERE id=?',(handle,))
    def prepare(self,name,args,principal):
        sid=self.require(principal)
        args=dict(args)
        from .tool_policy import scope_for
        from .control_plane_guard import PATH_KEYS
        if scope_for(name)!='mcp:read':
            with self.db() as c:
                rows=c.execute("SELECT value FROM handles WHERE session=? AND kind='worktree'",(sid,)).fetchall()
            if rows:
                roots=[Path(json.loads(row[0])).resolve() for row in rows]
                for key,value in args.items():
                    if key in PATH_KEYS and isinstance(value,str):
                        target=Path(value).resolve()
                        if not any(target==r or r in target.parents for r in roots):
                            raise PermissionError("SESSION_WORKTREE: writes must stay in the session worktree")
        if name in {"process_details","process_stop"}: self.require_pid(sid,args["pid"])
        if name.startswith("uia_") and "target_hwnd" in args:
            self.bind(sid,"hwnd",int(args["target_hwnd"]))
        if name.startswith("eda_sch_worker_"):
            self.bind(sid,"eda_worker","singleton")
        if name in {'uia_control_focus','uia_control_window','uia_control_scroll','uia_control_reveal','uia_desktop_windows','uia_desktop_recover','uia_desktop_release','uia_invoke','uia_set_value','uia_toggle','uia_control_snapshot','uia_control_find','uia_control_state','uia_control_select','uia_control_expand_collapse','uia_control_set_range_value'}:
            ref=args['target_ref']
            args['target_ref']={**self.resolve(sid,'uia_control_target',ref),'lease_ref':ref}
        if name.startswith('uia_') and 'workspace_ref' in args:
            args['workspace_ref']=self.resolve(sid,'uia_workspace',args['workspace_ref'])
        for key in ("tab_ref","element_ref","root_ref"):
            if key in args and args[key] is not None:
                kind=("dom_" if name.startswith("dom_") else "uia_")+("element_ref" if key=="root_ref" else key)
                try:args[key]=self.resolve(sid,kind,args[key])
                except PermissionError:
                    if name!='dom_close_tab' or key!='tab_ref':raise
                    args[key]=self.resolve(sid,'dom_closed_tab_ref',args[key])
        return sid,args
    def output(self,name,value,sid):
        if name=="process_start" and isinstance(value,dict) and "pid" in value:
            import psutil
            self.bind(sid,"pid",[value["pid"],psutil.Process(value["pid"]).create_time()])
        def walk(item):
            if isinstance(item,list):
                result=[]
                for child in item:
                    try: result.append(walk(child))
                    except PermissionError: continue
                return result
            if isinstance(item,dict):
                out={}
                for k,v in item.items():
                    if k in {"tab_ref","element_ref","root_ref"} and isinstance(v,str):
                        kind=("dom_" if name.startswith("dom_") else "uia_")+("element_ref" if k=="root_ref" else k)
                        out[k]=self.bind(sid,kind,v)
                    else: out[k]=walk(v)
                return out
            return item
        if name=="process_list":
            value=dict(value)
            owned=[]
            for proc in value.get("processes",[]):
                try: self.require_pid(sid,proc["pid"]); owned.append(proc)
                except Exception: continue
            value["processes"]=owned
            value["ownership_filtered"]=True
        if name=="dom_close_tab" and isinstance(value,dict) and value.get("closed"):
            raw=value.get("tab_ref")
            with self.db() as c:
                c.execute("DELETE FROM handles WHERE session=? AND kind IN ('dom_tab_ref','dom_closed_tab_ref') AND value=?",
                          (sid,json.dumps(raw,sort_keys=True)))
            return value
        return walk(value) if name.startswith(("dom_","uia_")) else value


    def job_start(self,sid,tool):
        if tool not in {"powershell_run","command_profile_run","eda_kicad_erc","eda_kicad_drc",
                        "eda_sch_materialize","eda_sch_verify","eda_sch_worker_apply",
                        "eda_altium_library_inspect","eda_altium_sch_plan_create","eda_altium_sch_validate",
                        "eda_altium_sch_materialize","eda_altium_sch_verify",
                        "eda_altium_sch_inspect","eda_altium_sch_edit_preview","eda_altium_sch_edit_apply",
                        "eda_altium_sch_erc","eda_altium_sch_edit_commit","eda_altium_sch_edit_restore"}:
            return None
        job=secrets.token_urlsafe(24)
        with self.db() as c:
            c.execute("INSERT INTO jobs VALUES(?,?,?,?,?)",(job,sid,tool,"running",time.time()))
        return job
    def job_finish(self,job,status):
        if job is None: return
        with self.db() as c:
            c.execute("UPDATE jobs SET status=? WHERE id=?",(status,job))

    def _recover_operations(self,c):
        for ident,pid,started in c.execute("SELECT id,pid,started FROM operations").fetchall():
            if not self._runtime_alive(pid,started): c.execute("DELETE FROM operations WHERE id=?",(ident,))

    def acquire_operation(self,sid,tool,exclusive=False,tracking=False):
        if tracking and (exclusive or not tool.startswith('provider.dom.')):raise ValueError('Provider tracking requires an internal DOM operation')
        if not exclusive and tool in {'routing_policy_info','security_policy_info',
                'work_session_status','work_session_close_preview','task_attempt_status',
                'capability_readiness','connection_diagnostics','dom_operation_status','dom_operation_cancel','dom_operation_recover','dom_review_status','dom_review_record','dom_browser_choices'}:
            return None
        ident=secrets.token_urlsafe(24)
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._recover_operations(c)
            if exclusive:
                if c.execute("SELECT 1 FROM operations LIMIT 1").fetchone():
                    raise PermissionError("NODE_BUSY: active operations block restart/update")
                if c.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                    raise PermissionError("NODE_BUSY: running jobs block restart/update")
                if c.execute("SELECT 1 FROM attempts WHERE status='running' LIMIT 1").fetchone():
                    raise PermissionError("NODE_BUSY: unfinished attempts block restart/update")
                if c.execute("SELECT 1 FROM handles h JOIN sessions s ON h.session=s.id WHERE h.kind='test_run' OR (s.status='active' AND h.kind IN ('dom_tab_ref','pid')) LIMIT 1").fetchone():
                    raise PermissionError("NODE_BUSY: close live resources before restart/update")
            elif c.execute("SELECT 1 FROM operations WHERE exclusive=1 LIMIT 1").fetchone():
                raise PermissionError("NODE_BUSY: restart/update transaction is active")
            if sid:
                state=c.execute("SELECT status FROM sessions WHERE id=?",(sid,)).fetchone()
                if not state or state[0]!='active': raise PermissionError("Session is not active")
            if tracking:
                if c.execute("SELECT COUNT(*) FROM operations WHERE tool LIKE 'provider.dom.%'").fetchone()[0]>=64:raise PermissionError('RESOURCE_LIMIT: provider tracking budget reached')
            if not exclusive and not tracking:
                if c.execute("SELECT COUNT(*) FROM operations WHERE tool NOT LIKE 'provider.dom.%'").fetchone()[0]>=self.limits['max_operations_total']:
                    raise PermissionError('RESOURCE_LIMIT: node operation budget reached')
                if sid and c.execute("SELECT COUNT(*) FROM operations WHERE session=? AND tool NOT LIKE 'provider.dom.%'",(sid,)).fetchone()[0]>=self.limits['max_operations_per_session']:
                    raise PermissionError('RESOURCE_LIMIT: session operation budget reached')
            c.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                (ident,sid,tool,int(exclusive),self.pid,self.started,time.time()))
        return ident

    def release_operation(self,ident):
        with self.db() as c: c.execute("DELETE FROM operations WHERE id=?",(ident,))

    def attempt_begin(self,sid,key,fingerprint,label=""):
        if not isinstance(key,str) or not 1<=len(key)<=128: raise ValueError("attempt_key requires 1..128 characters")
        with self.lock,self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            state=c.execute("SELECT status FROM sessions WHERE id=?",(sid,)).fetchone()
            if not state or state[0]!='active': raise PermissionError("Session is not active")
            row=c.execute("SELECT id,fingerprint,status,result FROM attempts WHERE session=? AND key=?",(sid,key)).fetchone()
            if row:
                if row[1]!=fingerprint: raise ValueError("ATTEMPT_CONFLICT: key reused for a different command")
                return {"id":row[0],"created":False,"status":row[2],"result":json.loads(row[3]) if row[3] else None}
            ident=secrets.token_urlsafe(24)
            c.execute("INSERT INTO attempts(id,session,key,fingerprint,status,cancel,result,runtime_id,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident,sid,key,fingerprint,'running',0,None,self.runtime_id,time.time()))
            c.execute('INSERT INTO attempt_labels VALUES(?,?)',(ident,str(label)[:120]))
            c.execute('INSERT INTO attempt_lifecycle VALUES(?,NULL,NULL)',(ident,))
        return {"id":ident,"created":True,"status":"running","phase":"received"}

    def attempt_start(self,ident):
        with self.lock,self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT status,cancel FROM attempts WHERE id=?',(ident,)).fetchone()
            if row != ('running',0):return False
            c.execute('UPDATE attempt_lifecycle SET started=? WHERE id=? AND started IS NULL',(time.time(),ident))
        return True

    def attempt_finish(self,ident,status,result):
        with self.db() as c:
            c.execute("UPDATE attempts SET status=?,result=? WHERE id=? AND status='running'",
                      (status,json.dumps(result),ident))
            c.execute('UPDATE attempt_lifecycle SET finished=? WHERE id=? AND finished IS NULL',(time.time(),ident))

    def attempt_cancelled(self,ident):
        with self.db() as c: row=c.execute("SELECT cancel FROM attempts WHERE id=?",(ident,)).fetchone()
        return bool(row and row[0])

    def attempt_cancel(self,owner,ident):
        with self.lock,self.db() as c:
            row=c.execute("SELECT a.status FROM attempts a JOIN sessions s ON a.session=s.id WHERE a.id=? AND s.owner=?",(ident,owner)).fetchone()
            if not row: raise PermissionError("Unknown or foreign attempt")
            if row[0]=='running': c.execute("UPDATE attempts SET cancel=1 WHERE id=?",(ident,))
        return {"attempt_id":ident,"status":row[0],"phase":"cancelling" if row[0]=='running' else 'finished',
                "cancel_requested":row[0]=='running',"stop_confirmed":row[0]=='cancelled'}

    def attempts_status(self,owner):
        with self.db() as c:
            rows=c.execute("SELECT a.id,a.session,a.status,a.cancel,a.created,l.started,l.finished,l.id FROM attempts a JOIN sessions s ON a.session=s.id LEFT JOIN attempt_lifecycle l ON l.id=a.id WHERE s.owner=? ORDER BY a.created DESC LIMIT 200",(owner,)).fetchall()
        return {"attempts":[{"attempt_id":i,"session_id":sid,"status":st,"cancel_requested":bool(cancel),"created":ts,
                "phase":'finished' if st!='running' else 'cancelling' if cancel else 'received' if lifecycle and started is None else 'running',
                "started":started,"finished":finished,"stop_confirmed":st=='cancelled'} for i,sid,st,cancel,ts,started,finished,lifecycle in rows]}
