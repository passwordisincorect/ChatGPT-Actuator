"""Bounded Windows directory signals are hints; every wake is verified by secured reads."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time


class DirectorySignals:
    def __init__(self):
        self.handles, self.pending, self.last = {}, set(), {}
        self.api = None
        if os.name == 'nt':
            api = ctypes.WinDLL('kernel32',use_last_error=True)
            api.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR,wintypes.BOOL,wintypes.DWORD]
            api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
            for name in ('FindNextChangeNotification','FindCloseChangeNotification'):
                getattr(api,name).argtypes = [wintypes.HANDLE]
                getattr(api,name).restype = wintypes.BOOL
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
            api.WaitForSingleObject.restype = wintypes.DWORD
            self.api = api

    def sync(self, roots):
        roots = set(sorted(roots)[:8])
        for root in list(self.handles):
            if root not in roots:
                self.api.FindCloseChangeNotification(self.handles.pop(root))
                self.pending.discard(root); self.last.pop(root,None)
        if self.api is None:
            return
        for root in roots - self.handles.keys():
            handle = self.api.FindFirstChangeNotificationW(root,True,0x1|0x2|0x8|0x10)
            if handle not in (None,ctypes.c_void_p(-1).value):
                self.handles[root] = handle

    def poll(self):
        for root,handle in list(self.handles.items()):
            result = self.api.WaitForSingleObject(handle,0)
            if result == 0:
                self.pending.add(root)
                if not self.api.FindNextChangeNotification(handle):
                    self.api.FindCloseChangeNotification(self.handles.pop(root))
            elif result == 0xffffffff:
                self.api.FindCloseChangeNotification(self.handles.pop(root))
        now, ready = time.monotonic(), []
        for root in list(self.pending):
            if now-self.last.get(root,-1e9) >= 5:
                ready.append(root); self.pending.remove(root); self.last[root]=now
        return ready

    def close(self):
        self.sync([])

    def status(self):
        return {'mode':'windows_directory_signals' if self.api else 'bounded_polling',
                'watched_roots':len(self.handles),'max_roots':8,'fallback_seconds':30}
