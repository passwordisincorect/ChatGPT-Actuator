"""Windows child-tree lifetime guard. No interactive input or window activation."""
import os

class WindowsJob:
    @classmethod
    def from_pid(cls,pid,limits=None):
        if os.name!='nt':
            raise RuntimeError('Windows browser job requires Windows')
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype=wintypes.HANDLE
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x100|0x1|0x1000,False,int(pid))
        if not handle:raise OSError(ctypes.get_last_error(),'Cannot open headless driver')
        try:return cls(SimpleNamespace(_handle=handle),limits)
        finally:api.CloseHandle(handle)

    def __init__(self,process,limits=None,*,allow_external_ui_reads=False):
        self.handle=None
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes as w
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),
                ('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
                ('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(n,ctypes.c_ulonglong) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self._extended_type=Extended
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.api.CreateJobObjectW.restype=w.HANDLE
        self.api.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];self.api.SetInformationJobObject.restype=w.BOOL
        self.api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.api.AssignProcessToJobObject.restype=w.BOOL
        self.api.CloseHandle.argtypes=[w.HANDLE];self.api.CloseHandle.restype=w.BOOL
        handle=self.api.CreateJobObjectW(None,None)
        if not handle:raise OSError(ctypes.get_last_error(),'Cannot create process job')
        from .resource_limits import validated
        budget=validated(limits)
        info=Extended();info.BasicLimitInformation.LimitFlags=0x2000|0x8|0x100|0x200
        info.BasicLimitInformation.ActiveProcessLimit=budget['max_child_processes']
        info.ProcessMemoryLimit=budget['process_memory_mb']*1024*1024
        info.JobMemoryLimit=budget['job_memory_mb']*1024*1024
        if not self.api.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not self.api.AssignProcessToJobObject(handle,int(process._handle)):
            error=ctypes.get_last_error();self.api.CloseHandle(handle)
            raise OSError(error,'Cannot isolate command child-tree; fallback forbidden')
        # Deny switching desktops, global input/UI state and clipboard access.
        # The fixed read-only UIA worker needs HWND access to the inspected app.
        # Other job clients retain all restrictions, including external handles.
        restrictions=w.DWORD(0xFE if allow_external_ui_reads else 0xFF)
        if not self.api.SetInformationJobObject(handle,4,ctypes.byref(restrictions),ctypes.sizeof(restrictions)):
            error=ctypes.get_last_error();self.api.CloseHandle(handle)
            raise OSError(error,'Cannot enforce process UI restrictions')
        class Cpu(ctypes.Structure):
            _fields_=[('ControlFlags',w.DWORD),('CpuRate',w.DWORD)]
        cpu=Cpu(0x1|0x4,budget['cpu_percent']*100)
        if not self.api.SetInformationJobObject(handle,15,ctypes.byref(cpu),ctypes.sizeof(cpu)):
            error=ctypes.get_last_error();self.api.CloseHandle(handle)
            raise OSError(error,'Cannot enforce CPU budget; fallback forbidden')
        self.handle=handle
    def contains_pid(self,pid):
        """Check the held Job, not ancestry or a PID file, for listener ownership."""
        if not self.handle or type(pid) is not int or pid<=0:return False
        import ctypes
        from ctypes import wintypes as w
        self.api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];self.api.OpenProcess.restype=w.HANDLE
        self.api.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)];self.api.IsProcessInJob.restype=w.BOOL
        process=self.api.OpenProcess(0x1000,False,pid)
        if not process:return False
        try:
            belongs=w.BOOL()
            return bool(self.api.IsProcessInJob(process,self.handle,ctypes.byref(belongs)) and belongs.value)
        finally:self.api.CloseHandle(process)
    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle);self.handle=None

    def accounting(self):
        """Job counters only; does not expose process identity or command lines."""
        if not self.handle:return {'active_processes':0}
        import ctypes
        from ctypes import wintypes as w
        class BasicAccounting(ctypes.Structure):
            _fields_=[(n,ctypes.c_longlong) for n in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')]+[(n,w.DWORD) for n in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]
        self.api.QueryInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
        self.api.QueryInformationJobObject.restype=w.BOOL
        basic=BasicAccounting();extended=self._extended_type()
        if not self.api.QueryInformationJobObject(self.handle,1,ctypes.byref(basic),ctypes.sizeof(basic),None):
            raise OSError(ctypes.get_last_error(),'Job accounting unavailable')
        if not self.api.QueryInformationJobObject(self.handle,9,ctypes.byref(extended),ctypes.sizeof(extended),None):
            raise OSError(ctypes.get_last_error(),'Job memory accounting unavailable')
        return {'active_processes':basic.ActiveProcesses,'total_processes':basic.TotalProcesses,
                'terminated_processes':basic.TotalTerminatedProcesses,
                'peak_process_memory_mb':round(extended.PeakProcessMemoryUsed/1048576,3),
                'peak_job_memory_mb':round(extended.PeakJobMemoryUsed/1048576,3)}
