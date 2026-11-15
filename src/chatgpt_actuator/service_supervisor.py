"""Own the private desktop and Job for exactly one owner-approved Study Coach service."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import time
import httpx
import psutil
from .service_control import initialize,claim,complete,secure_directory,caller_alive

class StudyService:
    def __init__(self,settings,*,launcher=None,listener=None,health=None):
        self.settings=settings;self.stack=None;self.proc=None;self.created=None;self.job=None
        self.launcher=launcher or self._launch
        self.listener=listener or self._listener
        self.health=health or self._health
        self.last_state='stopped'
    def _listener(self):
        return {c.pid for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port==8780}
    def _health(self):
        try:
            with httpx.Client(timeout=1,trust_env=False,follow_redirects=False) as client:
                response=client.get('http://127.0.0.1:8780/api/health')
                return response.status_code==200 and response.json().get('ok') is True
        except (httpx.HTTPError,ValueError):return False
    def _launch(self):
        if os.name!='nt':raise RuntimeError('Windows private desktop is required')
        from .background_process import private_desktop,Capture
        from .windows_process import WindowsProcess
        from .windows_job import WindowsJob
        stack=ExitStack()
        try:
            desktop=stack.enter_context(private_desktop())
            proc=WindowsProcess(self.settings['argv'],self.settings['cwd'],self.settings['environment'],desktop)
            # LIFO: close the Job before releasing process and desktop handles.
            stack.callback(proc.close)
            job=WindowsJob(proc,self.settings['resource_limits']);stack.callback(job.close)
            self.job=job
            proc.resume()
            out,err=Capture(proc.stdout,4096),Capture(proc.stderr,4096)
            self.capture=(out,err)
            return proc,stack
        except BaseException:
            if 'proc' in locals() and proc.poll() is None:
                try:proc.terminate()
                except Exception:pass
            stack.close();raise
    def _owned_alive(self):
        if self.proc is None or self.proc.poll() is not None:return False
        try:return psutil.Process(self.proc.pid).create_time()==self.created
        except psutil.NoSuchProcess:return False
    def status(self):
        listeners=self.listener()
        alive=self._owned_alive()
        if self.proc is not None and not alive:
            self._close();self.last_state='exited'
        owned=alive and bool(listeners) and all(self.job.contains_pid(pid) if self.job else pid==self.proc.pid for pid in listeners)
        return {'service':'study-coach','state':'running' if owned else 'starting' if alive else 'foreign_listener' if listeners else self.last_state,
                'managed':alive,'healthy':bool(owned and self.health()),'background_only':True,'port':8780}
    def start(self,cancel=lambda:False):
        current=self.status()
        if current['managed']:return {**current,'already_running':True}
        if cancel():raise PermissionError('Service caller is no longer active')
        if current['state']=='foreign_listener':raise PermissionError('Port is occupied by an unmanaged process')
        for name,expected in self.settings['source_sha256'].items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:
                raise PermissionError('Approved service source changed')
        self.proc,self.stack=self.launcher()
        self.last_state='starting'
        try:
            self.created=psutil.Process(self.proc.pid).create_time()
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if cancel():raise PermissionError('Service start was cancelled')
                current=self.status()
                if current['healthy']:return current
                if not current['managed']:raise RuntimeError('Service exited before readiness')
                time.sleep(.1)
            raise TimeoutError('Owned service did not become healthy')
        except BaseException:self.stop();raise
    def _close(self):
        owned=[]
        if self.job and self.proc:
            try:
                parent=psutil.Process(self.proc.pid)
                if parent.create_time()==self.created:
                    owned=[p for p in [parent,*parent.children(recursive=True)] if self.job.contains_pid(p.pid)]
            except psutil.NoSuchProcess:pass
        if self.stack:self.stack.close()
        _,alive=psutil.wait_procs(owned,timeout=3) if owned else ([],[])
        for capture in getattr(self,'capture',()):
            capture.thread.join(timeout=.5)
            if not capture.thread.is_alive():capture.pipe.close()
        self.capture=()
        self.stack=self.proc=self.created=self.job=None
        if alive:raise RuntimeError('Owned service did not finish stopping')
    def stop(self):
        # Job handle refers to the original child tree, never a PID read from disk.
        self._close();self.last_state='stopped'
        return {'service':'study-coach','state':'stopped','background_only':True}

def main(home):
    if os.name!='nt':raise RuntimeError('Windows service supervisor required')
    home=Path(home);settings=json.loads((home/'deployment/study-service.json').read_text())
    secure_directory(home/'deployment/services')
    mailbox=home/'deployment/services/mailbox.sqlite3';initialize(mailbox)
    import win32event,win32api,winerror
    identity=hashlib.sha256(os.path.normcase(str(home.resolve())).encode('utf-8')).hexdigest()[:24]
    mutex=win32event.CreateMutex(None,False,'Local\\ActuatorStudySupervisor_'+identity)
    if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:
        win32api.CloseHandle(mutex);return
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(mailbox)) as connection,connection:
        connection.execute("UPDATE requests SET state='done',result=? WHERE state='running'",
            (json.dumps({'state':'interrupted','service':'study-coach'}),))
    marker=home/'deployment/service-supervisor.json'
    from . import __version__
    marker.write_text(json.dumps({'pid':os.getpid(),'created':psutil.Process().create_time(),'version':__version__}))
    service=StudyService(settings)
    try:
        while True:
            item=claim(mailbox)
            if item:
                ident,action,pid,created=item
                try:
                    if not caller_alive(pid,created):result={'state':'cancelled','service':'study-coach'}
                    else:result=service.start(cancel=lambda:not caller_alive(pid,created)) if action=='start' else getattr(service,action)()
                except Exception as exc:result={'state':'failed','error_type':type(exc).__name__,'service':'study-coach'}
                complete(mailbox,ident,result)
            time.sleep(.1)
    finally:
        service.stop();marker.unlink(missing_ok=True);win32api.CloseHandle(mutex)
