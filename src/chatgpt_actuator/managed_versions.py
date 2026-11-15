"""Owner-local version slots with atomic selection and rollback on failed health check."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from .atomic_files import atomic_write_text

VERSION=re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,79}$')

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

class ManagedVersions:
    def __init__(self,home,sessions):
        self.home=Path(home).resolve();self.sessions=sessions
        self.root=self.home/'runtime-versions';self.root.mkdir(parents=True,exist_ok=True)
        self.pointer=self.home/'runtime-current.json'
        self.state=self.home/'runtime-update.json'
    def _slot(self,version):
        if not isinstance(version,str) or not VERSION.fullmatch(version):raise ValueError('Invalid typed version')
        return self.root/version
    def prepare(self,version,source):
        destination=self._slot(version)
        if destination.exists():raise FileExistsError('Immutable version already staged')
        source=Path(source).resolve(strict=True)
        if not (source/'src/chatgpt_actuator/server.py').is_file():raise ValueError('Invalid runtime source')
        temp=Path(tempfile.mkdtemp(prefix='.stage-',dir=self.root))
        try:
            shutil.copytree(source/'src',temp/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            manifest={str(p.relative_to(temp)).replace('\\','/'):digest(p) for p in (temp/'src').rglob('*') if p.is_file()}
            (temp/'manifest.json').write_text(json.dumps(manifest,sort_keys=True),encoding='utf-8')
            temp.rename(destination)
        finally:
            if temp.exists():shutil.rmtree(temp)
        return {'version':version,'files':len(manifest),'slot':str(destination)}
    def verify(self,version):
        slot=self._slot(version)
        manifest=json.loads((slot/'manifest.json').read_text())
        actual={str(p.relative_to(slot)).replace('\\','/') for p in (slot/'src').rglob('*') if p.is_file()}
        if set(manifest)!=actual or 'src/chatgpt_actuator/server.py' not in manifest:raise ValueError('Incomplete version manifest')
        for name,expected in manifest.items():
            path=(slot/name).resolve(strict=True)
            if slot.resolve() not in path.parents or digest(path)!=expected:raise ValueError('Version slot checksum mismatch')
        return slot
    def activate(self,version,restart,healthy,*,existing_lease=None):
        self.verify(version)
        owned=existing_lease is None
        lease=self.sessions.acquire_operation(None,'node.update',exclusive=True) if owned else existing_lease
        if not owned:
            with self.sessions.db() as c:
                row=c.execute('SELECT exclusive,pid,started FROM operations WHERE id=?',(lease,)).fetchone()
            if row!=(1,self.sessions.pid,self.sessions.started):raise PermissionError('Invalid owner update lease')
        previous=self.pointer.read_text() if self.pointer.exists() else None
        def state(status):atomic_write_text(self.state,json.dumps({'version':version,'status':status,'previous':previous}))
        try:
            state('applying')
            atomic_write_text(self.pointer,json.dumps({'version':version}))
            restart()
            if not healthy():raise RuntimeError('Runtime health verification failed')
            state('succeeded')
            return {'version':version,'status':'succeeded'}
        except BaseException:
            if previous is None:self.pointer.unlink(missing_ok=True)
            else:atomic_write_text(self.pointer,previous)
            state('rolled_back')
            try:restart()
            except Exception:state('rollback_restart_failed')
            raise
        finally:
            if owned:self.sessions.release_operation(lease)

    def recover_interrupted(self):
        if not self.state.exists():return {'status':'idle'}
        state=json.loads(self.state.read_text())
        if state['status']!='applying':return state
        lease=self.sessions.acquire_operation(None,'node.recover',exclusive=True)
        try:
            previous=state.get('previous')
            if previous is None:self.pointer.unlink(missing_ok=True)
            else:atomic_write_text(self.pointer,previous)
            state['status']='rolled_back_after_interruption'
            atomic_write_text(self.state,json.dumps(state))
            return state
        finally:self.sessions.release_operation(lease)
