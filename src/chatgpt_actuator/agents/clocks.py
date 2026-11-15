"""Boot-scoped duration accounting, independent of adjustable calendar time."""
import os
import json
import time
from functools import lru_cache

import psutil


@lru_cache(maxsize=1)
def boot_id():
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                    r'SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters') as key:
                return 'windows:' + str(winreg.QueryValueEx(key, 'BootId')[0])
        except OSError:
            pass
    else:
        try:
            with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as stream:
                return stream.read().strip()
        except OSError:
            pass
    # Conservative fallback: a new process after a clock adjustment may invalidate
    # old accounting. Never reset consumed budget on an uncertain boot identity.
    return 'boot:' + str(round(psutil.boot_time()))


def point():
    return {'boot': boot_id(), 'tick': time.monotonic(), 'wall': time.time()}


def advance(doc):
    if not doc.get('started_at'):
        return
    now = point()
    old = doc.get('_duration_clock')
    if old is None:
        consumed = max(0, now['wall'] - doc['started_at'])
    elif old['boot'] != now['boot'] or now['tick'] < old['tick']:
        consumed = doc['max_seconds']  # Uncertain reboot cannot grant more time.
        doc['clock_issue'] = 'duration_boot_changed'
    else:
        consumed = old['elapsed'] + now['tick'] - old['tick']
        if abs((now['wall'] - old['wall']) - (now['tick'] - old['tick'])) > 30:
            doc['clock_issue'] = 'calendar_clock_changed'
    doc['_duration_clock'] = {**now, 'elapsed': consumed}


def remaining(doc):
    advance(doc)
    return max(0, doc['max_seconds'] - doc['_duration_clock']['elapsed'])


def timer(deadline):
    now = point()
    return {**now, 'delay': max(0, deadline - now['wall'])}


def ready(saved, deadline):
    now = point()
    if now['wall'] < deadline:
        return False
    if saved is None:
        return True  # Legacy absolute schedules retain their original semantics.
    if saved['boot'] != now['boot'] or now['tick'] < saved['tick']:
        return False  # Explicit rebase needed after reboot; never guess early.
    return now['tick'] - saved['tick'] >= saved['delay']


def accounting_day(db):
    db.execute('CREATE TABLE IF NOT EXISTS agent_budget_clock(singleton INTEGER PRIMARY KEY,document TEXT)')
    row = db.execute('SELECT document FROM agent_budget_clock WHERE singleton=1').fetchone()
    now = point()
    old = json.loads(row[0]) if row else None
    utc = now['wall'] if old is None else old['utc']
    if old and old['boot'] == now['boot'] and now['tick'] >= old['tick']:
        utc += now['tick'] - old['tick']
    # Across an uncertain reboot, retain the old quota day, then advance by
    # elapsed uptime. A calendar change must never grant another daily quota.
    db.execute('INSERT OR REPLACE INTO agent_budget_clock VALUES(1,?)',
               (json.dumps({**now, 'utc': utc}),))
    return int(utc // 86400)
