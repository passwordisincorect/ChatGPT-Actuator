"""Serialize cooperating writers and atomically publish file contents."""
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import weakref

class FilePreconditionRequired(ValueError):
    pass

class FileConflictError(RuntimeError):
    pass

_local_guard=threading.RLock()
_locks=weakref.WeakValueDictionary()

@contextmanager
def file_lock(path):
    key=os.path.normcase(str(Path(path).resolve()))
    with _local_guard:
        lock=_locks.setdefault(key,threading.RLock())
    with lock:
        directory=Path(tempfile.gettempdir())/"chatgpt-actuator-file-locks"
        directory.mkdir(exist_ok=True)
        lockpath=directory/(hashlib.sha256(key.encode()).hexdigest()+".lock")
        with lockpath.open("a+b") as handle:
            handle.seek(0,2)
            if handle.tell()==0: handle.write(b"0"); handle.flush()
            if os.name=="nt":
                import msvcrt
                deadline=time.monotonic()+10
                while True:
                    handle.seek(0)
                    try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1); break
                    except OSError:
                        if time.monotonic()>=deadline: raise TimeoutError("File writer lock busy")
                        time.sleep(.02)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX)
            try: yield
            finally:
                handle.seek(0)
                if os.name=="nt": msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else: fcntl.flock(handle,fcntl.LOCK_UN)

def serialized_write(fn):
    @wraps(fn)
    def checked(self,path,*args,**kwargs):
        target=self.guard.resolve_target(path)
        with file_lock(target):
            return fn(self,path,*args,**kwargs)
    return checked

def check_expected(path,expected):
    if expected is None: return
    if not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
        raise ValueError("expected_sha256 must be 64 hex characters")
    actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    if actual != expected.lower():
        raise FileConflictError("File changed or missing; read the current version before retrying")

def atomic_write(path,data,*,overwrite,expected=None):
    if overwrite and expected is not None:
        # Validate the input format without a separate, racy filesystem read.
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
            raise ValueError("expected_sha256 must be 64 hex characters")
        from .windows_atomic import replace_expected
        for attempt in range(8):
            try:
                replace_expected(path,data,expected,conflict_error=FileConflictError)
                return hashlib.sha256(data).hexdigest()
            except PermissionError as exc:
                if attempt==7 or getattr(exc,'winerror',None) not in {None,5,32,33}:raise
                time.sleep(.01*(2**attempt))
    fd,temp=tempfile.mkstemp(prefix=".actuator-write-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        for attempt in range(8):
            # Sharing violations can occur after the read handle closes (for
            # example while Windows scans the file). Recheck CAS before every
            # failed-rename retry so a newer owner edit can never be replaced.
            check_expected(path,expected)
            try:
                if overwrite:
                    os.replace(temp,path)
                else:
                    # Atomic create: a concurrent creator must never be overwritten.
                    os.link(temp,path)
                break
            except PermissionError as exc:
                if attempt==7 or getattr(exc,'winerror',None) not in {None,5,32,33}:raise
                time.sleep(.01*(2**attempt))
        return hashlib.sha256(data).hexdigest()
    finally:
        Path(temp).unlink(missing_ok=True)

def atomic_write_text(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with file_lock(path):
        return atomic_write(path,text.encode('utf-8'),overwrite=True)
