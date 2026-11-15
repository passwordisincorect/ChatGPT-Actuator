"""Create a command suspended on a private desktop, before assigning its Job."""
import msvcrt
import os
import subprocess
from threading import RLock
import win32con
import win32event
import win32process

_launch_lock=RLock()
class WindowsProcess:
    def __init__(self,argv,cwd,env,desktop):
        self.argv=argv;self.returncode=None
        # pywin32 serializes {} as a single ANSI NUL, which CreateProcessW
        # rejects. Supply only the required OS location, never the full parent
        # environment (which may contain credentials or unapproved variables).
        env=dict(env)
        if not any(key.casefold()=='systemroot' for key in env):
            system_root=os.environ.get('SystemRoot')
            if not system_root:raise RuntimeError('Windows SystemRoot is unavailable')
            env['SystemRoot']=system_root
        out_r,out_w=os.pipe();err_r,err_w=os.pipe()
        null=os.open(os.devnull,os.O_RDONLY)
        try:
            with _launch_lock:
                for fd in (out_w,err_w,null):os.set_inheritable(fd,True)
                startup=win32process.STARTUPINFO()
                startup.dwFlags=win32con.STARTF_USESTDHANDLES|win32con.STARTF_USESHOWWINDOW
                startup.wShowWindow=win32con.SW_HIDE
                startup.lpDesktop=desktop
                startup.hStdInput=msvcrt.get_osfhandle(null)
                startup.hStdOutput=msvcrt.get_osfhandle(out_w)
                startup.hStdError=msvcrt.get_osfhandle(err_w)
                hp,ht,pid,tid=win32process.CreateProcess(argv[0],subprocess.list2cmdline(argv),None,None,True,
                    win32con.CREATE_SUSPENDED|win32con.CREATE_NO_WINDOW|win32con.CREATE_UNICODE_ENVIRONMENT,env,cwd,startup)
                self._process_handle,self._thread_handle=hp,ht
                self._handle=int(hp);self.pid=pid
        except BaseException:
            os.close(out_r);os.close(err_r)
            raise
        finally:
            for fd in (out_w,err_w,null):os.close(fd)
        self.stdout=os.fdopen(out_r,'rb');self.stderr=os.fdopen(err_r,'rb')
    def resume(self):
        win32process.ResumeThread(self._thread_handle)
        self._thread_handle.Close();self._thread_handle=None
    def terminate(self):
        win32process.TerminateProcess(self._process_handle,1)
    def poll(self):
        if self.returncode is None and win32event.WaitForSingleObject(self._process_handle,0)==win32con.WAIT_OBJECT_0:
            self.returncode=win32process.GetExitCodeProcess(self._process_handle)
        return self.returncode
    def wait(self,timeout=None):
        ms=win32event.INFINITE if timeout is None else max(1,int(timeout*1000))
        if win32event.WaitForSingleObject(self._process_handle,ms)!=win32con.WAIT_OBJECT_0:
            raise subprocess.TimeoutExpired(self.argv,timeout)
        self.returncode=win32process.GetExitCodeProcess(self._process_handle)
        return self.returncode
    def close(self):
        if self._thread_handle:self._thread_handle.Close();self._thread_handle=None
        if self._process_handle:self._process_handle.Close();self._process_handle=None
