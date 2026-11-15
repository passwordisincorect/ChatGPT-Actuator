"""Bound the caller wait while retaining provider cleanup evidence/leases."""
import asyncio
import time
from .tool_policy import scope_for

DEADLINE_SECONDS=30.0
CLEANUP_WAIT_SECONDS=.2
_pending=set()

def _retain(task):
    _pending.add(task)
    def done(completed):
        _pending.discard(completed)
        if not completed.cancelled():completed.exception()
    task.add_done_callback(done)

async def run_step(index,tool,arguments,handler,runner,deadline):
    began=time.monotonic()
    remaining=deadline-time.monotonic()
    started=remaining>0
    if started:
        task=asyncio.create_task(runner(index,tool,arguments,handler,use_thread=False))
        _retain(task)
        try:
            complete,_=await asyncio.wait({task},timeout=remaining)
            if complete:return await task
            task.cancel()
            await asyncio.wait({task},timeout=CLEANUP_WAIT_SECONDS)
        except BaseException:
            task.cancel()
            raise
    read=scope_for(tool)=='mcp:read'
    return {'index':index,'tool':tool,'ok':False,'duration_ms':round((time.monotonic()-began)*1000,3),'error':{'type':'FlowDeadlineExceeded','message':'Verified DOM flow deadline exceeded','diagnostic':{'schema_version':1,'code':'DEADLINE_EXCEEDED','tool':tool,'delivery_state':'not_mutating' if read else 'unknown' if started else 'not_dispatched','phase':'failed','retry_safe':False,'next_action':'Inspect owned DOM operation status and target state; cancellation never undoes or replays actions'}},'provider_cleanup':'may_be_pending' if started else 'not_started'}
