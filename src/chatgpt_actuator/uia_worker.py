"""Disposable read-only UIA worker with an external deadline and Windows Job."""
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

ACTION_METHODS=frozenset({'control_action','control_read','workspace_windows','workspace_bind'})
READ_METHODS = frozenset({'snapshot', 'list_elements', 'find_elements', 'details'})


class UIAWorkerService:
    def __init__(self, config, audit, timeout_seconds=8):
        self.config, self.audit = config, audit
        self.timeout_seconds = min(15, max(1, float(timeout_seconds)))
        self._lock = threading.Lock()
        self.resource_limits = None
        from .snapshot_cache import SnapshotCache
        from collections import deque
        self._snapshots=SnapshotCache()
        self._samples=deque(maxlen=128)

    def snapshot_diff(self,target_hwnd,previous_snapshot_ref,max_depth=None,max_items=None,include_hidden=False,compact=False,max_changes=100):
        from .snapshot_cache import owner_scope
        scope=owner_scope(target_hwnd)
        self._snapshots.get(previous_snapshot_ref,scope)
        current=self._call('snapshot',(target_hwnd,),{'max_depth':max_depth,'max_items':max_items,'include_hidden':include_hidden})
        return self._snapshots.diff(previous_snapshot_ref,current['snapshot_ref'],scope,compact=compact,max_changes=max_changes)

    def metrics(self):
        samples=list(self._samples)
        values=sorted(sample['duration_ms'] for sample in samples)
        return {'samples':len(values),'p50_ms':values[len(values)//2] if values else None,
                'p95_ms':values[min(len(values)-1,int(len(values)*.95))] if values else None,
                'timeouts':sum(s['state']=='timeout' for s in samples),
                'failures':sum(s['state']=='failed' for s in samples),
                'worker_running':self._lock.locked(),'timeout_seconds':self.timeout_seconds,
                'sample_window':128,'labels_included':False,'percentiles_provisional':len(values)<20,
                'peak_process_rss_mb':max((s.get('peak_process_rss_mb',0) for s in samples),default=0)}

    def __getattr__(self, method):
        if method not in READ_METHODS | ACTION_METHODS:
            raise PermissionError('BACKGROUND_ONLY: UIA worker supports reads only')
        return lambda *args, **kwargs: self._call(method, args, kwargs)

    def _call(self, method, args, kwargs):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('UIA_BUSY: another bounded capture is running')
        from .windows_process import WindowsProcess
        from .windows_job import WindowsJob
        from .background_process import Capture
        from .resource_limits import validated
        process = job = None
        began = time.monotonic()
        state='failed'
        peak_rss=0
        try:
            payload = json.dumps({'method':method, 'args':args, 'kwargs':kwargs,
                                  'config':asdict(self.config)}, ensure_ascii=True)
            if len(payload.encode()) > 65536:
                raise ValueError('UIA request exceeds 64 KiB')
            with tempfile.TemporaryDirectory(prefix='actuator-uia-') as directory:
                request = Path(directory)/'request.json'
                request.write_text(payload, encoding='utf-8')
                env = {k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','PATH','LOCALAPPDATA','APPDATA','USERPROFILE') if k in os.environ}
                env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                           PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
                executable = Path(sys.executable).with_name('python.exe')
                process = WindowsProcess([str(executable), '-B', '-m',
                    'chatgpt_actuator.uia_worker', str(request)], directory, env, args[0].get('desktop') if method in ACTION_METHODS else None)
                job = WindowsJob(process, validated(self.resource_limits), allow_external_ui_reads=True)
                out, err = Capture(process.stdout, 2*1024*1024), Capture(process.stderr, 16384)
                process.resume()
                deadline = began+self.timeout_seconds
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        import psutil
                        peak_rss=max(peak_rss,psutil.Process(process.pid).memory_info().rss)
                    except psutil.Error:pass
                    time.sleep(.02)
                if process.poll() is None:
                    job.close(); job = None
                    process.wait(timeout=2)
                    raise TimeoutError('UIA_TIMEOUT: worker terminated; retry starts a fresh worker')
                out.thread.join(timeout=1); err.thread.join(timeout=1)
                raw = bytes(out.data)
                if len(raw) >= 2*1024*1024:
                    raise RuntimeError('UIA_OUTPUT_LIMIT: capture is too large')
                response = json.loads(raw.decode('utf-8'))
                if not response.get('ok'):
                    from .automation_errors import MutationOutcomeUnknown,PolicyDenied
                    kind = response.get('error_type')
                    from .uia_conditions import UIAPreconditionError,UIAValidationError,UIAAmbiguousTarget,UIASearchIncomplete
                    custom={c.__name__:c for c in (UIAPreconditionError,UIAValidationError,UIAAmbiguousTarget,UIASearchIncomplete)}
                    if kind in custom:raise custom[kind]('UIA guarded operation refused')
                    if kind=='PolicyDenied':raise PolicyDenied(response.get('reason','local_policy'),response.get('required_scope'))
                    error = {'ValueError':ValueError,'LookupError':LookupError,
                             'PermissionError':PermissionError,'MutationOutcomeUnknown':MutationOutcomeUnknown}.get(kind, RuntimeError)
                    raise error('UIA worker: '+str(response.get('message','failed'))[:512])
                self.audit.record('uia_worker_completed', method=method,
                    duration_ms=round((time.monotonic()-began)*1000))
                result=response['result']
                if method=='snapshot':
                    result['capture_options']={'include_hidden':bool(kwargs.get('include_hidden',False))}
                    from .snapshot_cache import owner_scope
                    target=args[0] if args else kwargs['target_hwnd']
                    result['snapshot_ref']=self._snapshots.store(owner_scope(target),result)
                state='completed'
                return result
        except TimeoutError:
            state='timeout'
            raise
        finally:
            self._samples.append({'state':state,'duration_ms':round((time.monotonic()-began)*1000,3),
                                  'peak_process_rss_mb':round(peak_rss/1048576,2)})
            if job: job.close()
            elif process and process.poll() is None: process.terminate()
            if process: process.close()
            if process:
                process.stdout.close(); process.stderr.close()
            self._lock.release()


def main():
    from .audit import AuditLogger
    from .config import UIAutomationConfig
    from .tools.ui_automation import UIAutomationService
    try:
        request = Path(sys.argv[1])
        if request.stat().st_size > 65536: raise ValueError('Request too large')
        data = json.loads(request.read_text(encoding='utf-8'))
        if data['method'] not in READ_METHODS | ACTION_METHODS: raise PermissionError('Read-only worker')
        config = UIAutomationConfig(**data['config'])
        service = UIAutomationService(config, AuditLogger(False,'unused.log'))
        if data['method']=='workspace_windows':
            import win32gui,win32process
            windows=[]
            def collect(hwnd,_):
                windows.append({'target_hwnd':hwnd,'pid':win32process.GetWindowThreadProcessId(hwnd)[1],'name':win32gui.GetWindowText(hwnd)[:256], 'class_name':win32gui.GetClassName(hwnd),'visible':bool(win32gui.IsWindowVisible(hwnd)),'owner_hwnd':win32gui.GetWindow(hwnd,4)})
            win32gui.EnumWindows(collect,None);result={'windows':windows}
        elif data['method']=='workspace_bind':
            from .uia_control import bind
            desktop_control=bool(data['args'][0].get('desktop_control'))
            result=bind(data['args'][1],private=not desktop_control,desktop_control=desktop_control)
        elif data['method']=='control_read':
            from .uia_control import validate
            target,method=data['args'];hwnd=validate(target)
            if method=='state':
                from .uia_state import read
                with service._com_scope():result=read(service,service._resolve_ref(hwnd,data['kwargs']['read_ref']))
            elif method=='find_unique':
                from .uia_conditions import filter_snapshot
                root_ref=data['kwargs'].get('root_ref')
                result=filter_snapshot(service.snapshot(hwnd,max_depth=data['kwargs'].get('max_depth',8),max_items=service.config.max_items,include_hidden=True,content_only=not data['kwargs'].get('include_chrome',False),root_ref=root_ref),data['kwargs']['query'])
                result['scope']='subtree' if root_ref else 'window_content_excluding_titlebar_and_menubar'
            else:
                if method not in READ_METHODS:raise PermissionError('Unsupported workspace read')
                result=getattr(service,method)(hwnd,**data['kwargs'])
        elif data['method']=='control_action':
            from dataclasses import replace
            service.config=replace(service.config,allow_invoke=True,allow_set_value=True,allow_toggle=True,allow_select=True,allow_expand_collapse=True,allow_range_value=True,allow_scroll=True,allow_scroll_into_view=True,allow_window_action=True)
            from .uia_control import perform
            result=perform(service,*data['args'],**data['kwargs'])
        else:result = getattr(service, data['method'])(*data['args'], **data['kwargs'])
        response = {'ok':True,'result':result}
    except Exception as exc:
        response = {'ok':False,'error_type':type(exc).__name__, 'message':str(exc)[:512]}
        from .automation_errors import PolicyDenied
        if isinstance(exc,PolicyDenied):response.update(reason=exc.reason,required_scope=exc.required_scope)
    raw = json.dumps(response, ensure_ascii=True)
    if len(raw.encode()) >= 2*1024*1024:
        raw = json.dumps({'ok':False,'message':'UIA_OUTPUT_LIMIT'})
    print(raw, flush=True)


if __name__ == '__main__': main()
