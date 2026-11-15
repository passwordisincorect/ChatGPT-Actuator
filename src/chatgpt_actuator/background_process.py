"""Bounded capture and a private, non-input Windows desktop for approved tasks."""
from contextlib import contextmanager
import os
import subprocess
import threading
import time
import signal
import psutil
from .windows_job import WindowsJob

@contextmanager
def private_desktop():
    if os.name!='nt':
        yield None
        return
    import ctypes
    from ctypes import wintypes
    import secrets
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.CreateDesktopW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p]
    api.CreateDesktopW.restype=wintypes.HANDLE
    api.CloseDesktop.argtypes=[wintypes.HANDLE];api.CloseDesktop.restype=wintypes.BOOL
    name='ActuatorBackground_'+secrets.token_hex(12)
    handle=api.CreateDesktopW(name,None,None,0,0xC7,None)
    if not handle: raise OSError(ctypes.get_last_error(),'Cannot create private desktop; visible fallback forbidden')
    try: yield 'winsta0\\'+name
    finally: api.CloseDesktop(handle)

def kill_tree(pid):
    if os.name!='nt':
        try: os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError: pass
        return

    try:
        root=psutil.Process(pid);children=root.children(recursive=True)
    except psutil.NoSuchProcess: return
    for child in reversed(children):
        try: child.kill()
        except psutil.NoSuchProcess: pass
    try: root.kill()
    except psutil.NoSuchProcess: pass
    psutil.wait_procs(children+[root],timeout=2)

class Capture:
    def __init__(self,pipe,limit):
        self.pipe,self.limit=pipe,limit
        self.data=bytearray();self.total=0;self.lock=threading.Lock()
        self.thread=threading.Thread(target=self.read,daemon=True)
        self.thread.start()
    def read(self):
        try:
            while True:
                block=self.pipe.read1(65536)
                if not block: break
                with self.lock:
                    self.total+=len(block)
                    remaining=self.limit-len(self.data)
                    if remaining>0:self.data.extend(block[:remaining])
        except (OSError,ValueError): pass
    def snapshot(self):
        with self.lock:return bytes(self.data),self.total>self.limit

def run_bounded(argv,cwd,env,timeout,limit,cancel=lambda:False,*,limits=None):
    began=time.monotonic();timed_out=cancelled=False
    with private_desktop() as startup:
        if os.name=='nt':
            from .windows_process import WindowsProcess
            proc=WindowsProcess(argv,cwd,env,startup)
        else:
            proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,start_new_session=True)
        try: job=WindowsJob(proc,limits)
        except BaseException:
            if os.name=="nt":
                if proc.poll() is None:proc.terminate()
            else:kill_tree(proc.pid)
            proc.stdout.close();proc.stderr.close()
            if os.name=="nt":proc.close()
            raise
        if os.name=="nt":
            try:proc.resume()
            except BaseException:
                job.close();proc.stdout.close();proc.stderr.close();proc.close()
                raise
        out,err=Capture(proc.stdout,limit),Capture(proc.stderr,limit)
        try:
            while proc.poll() is None:
                if cancel():cancelled=True;break
                if time.monotonic()-began>=timeout:timed_out=True;break
                time.sleep(.03)
            if timed_out or cancelled:
                if os.name=="nt":job.close()
                else:kill_tree(proc.pid)
            proc.wait(timeout=3)
            out.thread.join(timeout=.5);err.thread.join(timeout=.5)
            # Descendants retaining pipes cannot keep a request blocked forever.
            job.close()
            if os.name!="nt":kill_tree(proc.pid)
            out.thread.join(timeout=.5);err.thread.join(timeout=.5)
            stdout,ot=out.snapshot();stderr,et=err.snapshot()
            return {'exit_code':proc.returncode,'timed_out':timed_out,'cancelled':cancelled,
                'stdout':stdout.decode('utf-8',errors='replace'),'stderr':stderr.decode('utf-8',errors='replace'),
                'output_truncated':ot or et,'duration_seconds':round(time.monotonic()-began,3),
                'background_only':True}
        finally:
            job.close()
            if os.name!="nt" and proc.poll() is None:kill_tree(proc.pid)
            # Do not block in close() while a daemon reader owns a pipe lock.
            if not out.thread.is_alive():proc.stdout.close()
            if not err.thread.is_alive():proc.stderr.close()
            if os.name=="nt":proc.close()
