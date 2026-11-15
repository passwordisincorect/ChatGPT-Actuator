"""Observe top-level follow-up windows of the exact bound process, no input."""
import psutil
import time
import win32gui
import win32process
import pywintypes


def inventory(target,include_children=True):
    process = psutil.Process(target['pid'])
    if process.create_time() != target['started']:
        raise ValueError('Bound process identity changed')
    result = {}
    from .uia_process_family import capture,still_related,BROWSERS
    family,truncated=capture(target,include_children)
    def collect(hwnd, _):
        try:
            pid=win32process.GetWindowThreadProcessId(hwnd)[1];row=family.get(pid)
            if row and win32gui.IsWindowVisible(hwnd) and still_related(row,family):
                result[str(hwnd)] = {'hwnd': hwnd, 'pid': pid, 'started':row['started'],'class': win32gui.GetClassName(hwnd), 'title': win32gui.GetWindowText(hwnd)[:512],
                    'process_relationship':'bound_process' if pid==target['pid'] else 'verified_descendant','parent_pid':row['parent_pid'],'recommended_backend':'dom' if (row.get('name') or '').casefold() in BROWSERS else 'uia'}
        except pywintypes.error:pass
        return True
    win32gui.EnumWindows(collect, None)
    if psutil.Process(target['pid']).create_time() != target['started']:
        raise ValueError('Bound process identity changed')
    return {'windows':result,'process_inventory_truncated':truncated,'observed_processes':len(family)}

def windows(target,include_children=True):
    return inventory(target,include_children)['windows']


def delta(target, before):
    try:
        current=inventory(target);after = current['windows']
    except (psutil.Error, ValueError):
        return {'state': 'unconfirmed', 'automatic_action': False}
    new = [v for k, v in after.items() if k not in before]
    changed = [v for k, v in after.items() if k in before and v != before[k]]
    return {'state': 'observed', 'appeared': new, 'changed': changed,
            'disappeared': [int(k) for k in before if k not in after],
            'automatic_action': False, 'next_step': 'Bind an observed window and take a fresh UIA snapshot before acting',
            'scope': 'bound_process_and_verified_descendants', 'process_limit':32,'depth_limit':4,'process_inventory_truncated':current['process_inventory_truncated'],'business_outcome_verified': False}


def observe(target,before,timeout_ms=600):
    """Allow a bounded asynchronous dialog creation interval after dispatch."""
    if type(timeout_ms) is not int or not 0<=timeout_ms<=600:raise ValueError('Follow-up observation requires 0..600ms')
    began=time.monotonic();samples=0
    while True:
        samples+=1;result=delta(target,before)
        elapsed=(time.monotonic()-began)*1000
        if result['state']!='observed' or result['appeared'] or elapsed>=timeout_ms:
            return {**result,'samples':samples,'observed_ms':round(elapsed),'observation_limit_ms':timeout_ms}
        time.sleep(min(.05,max(0,(timeout_ms-elapsed)/1000)))
