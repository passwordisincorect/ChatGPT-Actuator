"""Authenticated local broker calls share MCP ownership and update leases."""
import asyncio
import hashlib
from dataclasses import replace
from threading import RLock
from .tool_policy import Principal, principal_context, authorize
from .work_sessions import WorkSessions

METHODS = {name: 'dom_' + name for name in ('list_tabs', 'open_tab', 'navigate', 'close_tab',
    'find', 'details', 'read_text', 'click', 'set_value', 'select', 'toggle',
    'scroll_into_view', 'scroll', 'wait','snapshot','snapshot_diff','accessibility_snapshot')}

class BrokerSessions:
    def __init__(self, runtime, host, token):
        self.runtime, self.host = runtime, host
        self.owner = 'local-broker-' + hashlib.sha256(token.encode()).hexdigest()[:24]
        self._sessions = None
        self._lock = RLock()

    @property
    def sessions(self):
        with self._lock:
            if self._sessions is None:
                limits = self.runtime.raw_config().get('security', {}).get('resource_limits')
                self._sessions = WorkSessions(self.runtime.config_path.parent / 'work-sessions.sqlite3', limits)
            return self._sessions

    def invoke(self, action, args, sid):
        sessions = self.sessions
        if action == 'session_open':
            if args or sid:
                raise ValueError('Session open takes no arguments or session')
            lease = sessions.acquire_operation(None, 'work_session_open')
            try:
                return sessions.open(self.owner)
            finally:
                sessions.release_operation(lease)
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            raise PermissionError('Broker Work Session is required')
        principal = Principal(owner=self.owner, session=sid)
        sessions.require(principal)
        if action == 'session_close':
            if args:
                raise ValueError('Session close takes no arguments')
            lease = sessions.acquire_operation(sid, 'work_session_close')
            try:
                from .session_resources import reconcile
                reconcile(sessions,self.host,self.owner,sid)
                result = sessions.close(self.owner, sid)
                if result.get('closed'):
                    self.host.call('release_session', self.owner, sid, timeout=10)
                return result
            finally:
                sessions.release_operation(lease)
        tool = METHODS.get(action)
        if tool is None:
            raise ValueError('Unsupported background broker action')
        authorize(tool, principal, args)
        _, prepared = sessions.prepare(tool, args, principal)
        if action=='open_tab':
            # This private broker exposes only isolated managed sessions, not
            # personal-browser attachment. Preserve that explicit channel contract.
            prepared.setdefault('browser_mode','managed')
        lease = sessions.acquire_operation(sid, tool)
        async def run():
            try:
                value = await self.host._invoke(action, **prepared)
                from .automation_contract import result
                value=result(tool,value)
                return sessions.output(tool, value, sid)
            finally:
                sessions.release_operation(lease)
        token = principal_context.set(replace(principal, session=sid))
        try:
            try:
                future = asyncio.run_coroutine_threadsafe(run(), self.host._require_loop())
            except Exception:
                sessions.release_operation(lease)
                raise
            # Cancellation keeps the lease until run() actually completes cleanup.
            try:return future.result(timeout=40)
            except TimeoutError:
                if not future.done():future.cancel()
                raise
        finally:
            principal_context.reset(token)
