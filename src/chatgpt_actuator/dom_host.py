from __future__ import annotations

import asyncio
from concurrent.futures import Future
import inspect
import time
import secrets
from threading import Event, Thread


class DOMAsyncProxy:
    def __init__(self, host: "DOMExecutionHost") -> None:
        self._host = host

    def __getattr__(self, name: str):
        async def caller(*args, **kwargs):
            return await self._host.acall(name, *args, **kwargs)
        return caller


class DOMExecutionHost:
    """Own one asyncio loop for all BrowserDOMService operations."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self.idle_seconds = 60.0
        self._active_calls = 0
        self._last_activity = time.monotonic()
        self._admission_lock = None
        self._idle_task = None
        self.deadline_seconds = 30.0
        self._operations = {}
        # Accessed only by the owned DOM loop; one registration per host.
        self._tracking_sessions = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._admission_lock = asyncio.Lock()
            self._idle_task = loop.create_task(self._idle_watch())
            self._ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = Thread(
            target=runner,
            name="ChatGPT-Actuator-DOM",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("DOM execution loop failed to start.")

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("DOM execution loop is not running.")
        return loop

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(min(5.0,max(.05,self.idle_seconds/2)))
            if self._active_calls or time.monotonic()-self._last_activity<self.idle_seconds:
                continue
            async with self._admission_lock:
                if self._active_calls:continue
                service=self.runtime.get_service('dom')
                if getattr(service,'_browser',None) is not None and not any(c.pages for c in service._session_contexts.values()):
                    try:await asyncio.wait_for(service.disconnect(),timeout=10)
                    except Exception:pass  # Retry on the next idle tick; never interrupt an active call.

    async def _invoke(self, method: str, *args, **kwargs):
        from .tool_policy import principal_context
        from .automation_errors import PolicyDenied,DOMOperationCancelled,DOMDeadlineExceeded
        principal=principal_context.get()
        owner=getattr(principal,'owner','local');sid=getattr(principal,'session','default')
        for entry in self._operations.values():
            task=entry['task']
            if task and task.cancelling() and entry['state'] in ('queued','running'):entry['state']='cancelling'
        if method=='operation_status':
            now=time.monotonic()
            return {'operations':[{k:v for k,v in entry.items() if k not in {'task','owner','session','began'}}|{'elapsed_seconds':round(now-entry['began'],3)} for entry in self._operations.values() if entry['owner']==owner and entry['session']==sid and (kwargs.get('include_finished',False) or entry['task'] is not None)], 'automatic_replay':False}
        if method in {'operation_cancel','operation_recover'}:
            ident=kwargs.get('operation_id');entry=self._operations.get(ident)
            if not entry or entry['owner']!=owner or entry['session']!=sid:raise PolicyDenied('wrong_owner')
            task=entry['task']
            requested=task is not None and not task.done()
            if method=='operation_recover':
                if entry['state']!='cancelling':raise ValueError('Cancel first and inspect pending cleanup before recovery')
                owned=[e['task'] for e in self._operations.values() if e['owner']==owner and e['session']==sid and e['task'] is not None]
                result=self.runtime.get_service('dom').recover_owned_transport(owner,sid)
                for pending in owned:pending.cancel()
                _,pending=await asyncio.wait(owned,timeout=2) if owned else (set(),set())
                return {**result,'operation_id':ident,'recovery_state':'pending' if pending else 'completed','requires_backend_restart':bool(pending),'automatic_replay':False}
            if task is not None and not task.done():
                entry['state']='cancelling';task.cancel()
                await asyncio.wait({task},timeout=.2)
            return {'operation_id':ident,'state':entry['state'],'cancel_requested':requested,'automatic_replay':False,'delivery_state':'unknown' if entry['dispatched'] else 'not_dispatched'}
        now=time.monotonic()
        self._operations={k:v for k,v in self._operations.items() if v['task'] is not None or now-v['began']<300}
        completed=[k for k,v in self._operations.items() if v['task'] is None]
        for k in completed[:-128]:self._operations.pop(k,None)
        ident=secrets.token_urlsafe(18)
        entry={'operation_id':ident,'method':method,'owner':owner,'session':sid,'began':now,'state':'queued','dispatched':False,'task':asyncio.current_task()}
        self._operations[ident]=entry
        entry['task'].add_done_callback(lambda task:None if task.cancelled() else task.exception())
        self._active_calls += 1
        provider_lease=None;tracking_sessions=None
        try:
            if hasattr(self.runtime,'config_path'):
                from .work_sessions import WorkSessions
                if self._tracking_sessions is None:
                    self._tracking_sessions=WorkSessions(self.runtime.config_path.parent/'work-sessions.sqlite3')
                tracking_sessions=self._tracking_sessions
                # Session teardown runs after WorkSessions.close commits the
                # closed state, so its cleanup lease must use the global
                # provider budget. Ordinary DOM calls remain session-scoped.
                lease_sid=None if sid=='default' or method=='release_session' else sid
                provider_lease=tracking_sessions.acquire_operation(lease_sid,'provider.dom.'+method,tracking=True)
            from .dom_attachment import attachment_wait
            async with asyncio.timeout(attachment_wait(method,args,kwargs,self.deadline_seconds)):
                async with self._admission_lock:
                    service = self.runtime.get_service('dom')
                    if hasattr(self.runtime,'config_path') and getattr(service,'resource_sessions',None) is None:
                        from .work_sessions import WorkSessions
                        service.resource_sessions=WorkSessions(self.runtime.config_path.parent/'work-sessions.sqlite3')
                    target = getattr(service, method)
                    guard=getattr(service,'guard_mutation',None)
                    if guard is not None:await guard(method)
                    entry['state']='running';entry['dispatched']=True
                    result = target(*args, **kwargs)
                    if inspect.isawaitable(result):result=await result
                    if entry['state']=='cancelling' or entry['task'].cancelling():raise DOMOperationCancelled(entry['dispatched'])
                    entry['state']='completed'
                    return result
        except TimeoutError:
            entry['state']='timed_out'
            raise DOMDeadlineExceeded(entry['dispatched']) from None
        except asyncio.CancelledError:
            entry['state']='cancelled'
            raise DOMOperationCancelled(entry['dispatched']) from None
        except DOMOperationCancelled:
            entry['state']='cancelled';raise
        except BaseException:
            entry['state']='failed';raise
        finally:
            try:
                if provider_lease:tracking_sessions.release_operation(provider_lease)
            finally:
                entry['task']=None
                self._active_calls -= 1
                self._last_activity = time.monotonic()

    def submit(self, method: str, *args, **kwargs) -> Future:
        return asyncio.run_coroutine_threadsafe(
            self._invoke(method, *args, **kwargs),
            self._require_loop(),
        )

    async def acall(self, method: str, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(method, *args, **kwargs))

    def call(self, method: str, *args, timeout: float = 40.0, **kwargs):
        future=self.submit(method,*args,**kwargs)
        try:return future.result(timeout=timeout)
        except TimeoutError:
            if not future.done():future.cancel()
            raise

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        try:
            self.call("disconnect", timeout=5.0)
        except Exception:
            pass
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        self._loop = None
        self._thread = None
        self._ready.clear()
