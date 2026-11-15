"""Independent owner-backend watchdog. Never restart a live/busy user workspace."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import psutil

from .coordination import health
from .queue import AgentQueue


def expected_script():
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'ChatGPT-Actuator-Autorecovery/http_mcp.py'


def identity(process, home):
    try:
        argv = process.cmdline()
        index = 1
        while index < len(argv) and argv[index].startswith('-'):
            if argv[index] in {'-c', '-m'}:
                return False
            index += 2 if argv[index] in {'-X', '-W'} else 1
        if index >= len(argv) or Path(argv[index]).resolve() != expected_script().resolve():
            return False
        launchers = {Path(home) / '.venv/Scripts/python.exe', Path(home) / '.venv/Scripts/pythonw.exe'}
        executable = Path(process.exe()).resolve()
        if Path(argv[0]).resolve() in launchers and executable in launchers:
            return True
        bases = {Path(sys.base_prefix) / 'python.exe', Path(sys.base_prefix) / 'pythonw.exe'}
        if executable not in bases or Path(argv[0]).resolve() not in bases:
            return False
        # Windows venv redirectors remain as a parent of the real interpreter.
        # Require the exact protected launcher chain, not a base Python name.
        parent = process.parent()
        return parent is not None and Path(parent.exe()).resolve() in launchers and identity(parent, home)
    except (OSError, ValueError, psutil.Error):
        return False


def start(home, worker):
    home = Path(home)
    if os.name != 'nt' or not identity(psutil.Process(), home):
        return False  # Never supervise ad hoc servers, tests, stdio, or another app.
    executable = home / '.venv/Scripts/pythonw.exe'
    env = dict(os.environ, PYTHONPATH=str(home / 'src'), CHATGPT_ACTUATOR_HOME=str(home),
               CHATGPT_ACTUATOR_CONFIG=str(home / 'config/config.json'), PYTHONDONTWRITEBYTECODE='1')
    subprocess.Popen([str(executable), '-B', '-m', 'chatgpt_actuator.agents.watchdog',
        str(home), str(os.getpid()), str(psutil.Process().create_time()), worker], cwd=home, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return True


@contextmanager
def admission():
    url = 'http://127.0.0.1:8189/update-lease'
    lease = None
    with httpx.Client(timeout=3, trust_env=False) as client:
        state = client.get(url)
        state.raise_for_status()
        current = state.json()
        if current.get('state') == 'paused':
            raise RuntimeError('Another owner update is active')
        csrf = current['csrf']
        prepared = client.post(url, headers={'Origin': 'http://127.0.0.1:8189'},
                               data={'csrf': csrf, 'action': 'prepare'})
        prepared.raise_for_status()
        lease = prepared.json()['lease']
        try:
            paused = client.get(url).json()
            if paused.get('state') != 'paused' or paused.get('active') or paused.get('pending_approvals'):
                raise RuntimeError('Outstanding requests block watchdog restart')
            yield
        finally:
            if lease:
                client.post(url, headers={'Origin': 'http://127.0.0.1:8189'},
                            data={'csrf': csrf, 'action': 'resume', 'lease': lease})


def decision(observed, process_matches, maintenance=False):
    if not process_matches:
        return 'identity_mismatch'
    if maintenance:
        return 'maintenance'
    if observed.get('state') != 'stalled':
        return 'no_restart'
    return 'restart_candidate'


def journal(store, state):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS agent_watchdog_events(seq INTEGER PRIMARY KEY,created REAL,state TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS agent_watchdog_restarts(id INTEGER PRIMARY KEY,created REAL,pid INTEGER,started REAL)')
        db.execute('INSERT INTO agent_watchdog_events(created,state) VALUES(?,?)', (time.time(), state))
        db.execute('DELETE FROM agent_watchdog_events WHERE seq NOT IN '
                   '(SELECT seq FROM agent_watchdog_events ORDER BY seq DESC LIMIT 1000)')


def attempt_restart(home, process, started, worker, queue):
    from ..owner_native_runner import NativeRunner
    from ..work_sessions import WorkSessions
    if process.create_time() != started or decision(health(queue.store, worker), identity(process, home)) != 'restart_candidate':
        return False
    with queue.store.db() as db:
        recent = db.execute('SELECT COUNT(*) FROM agent_watchdog_restarts WHERE created>?',
                            (time.time() - 3600,)).fetchone()[0]
    if recent >= 2:
        return False
    with admission():
        sessions = WorkSessions(Path(home) / 'config/work-sessions.sqlite3')
        lease = sessions.acquire_operation(None, 'node.agent_watchdog', exclusive=True)
        try:
            if process.create_time() != started or not identity(process, home) or health(queue.store, worker)['state'] != 'stalled':
                return False
            version = json.loads((Path(home) / 'runtime-current.json').read_text())['version']
            runner = NativeRunner(home, version)
            runner.integrity()
            with queue.store.db() as db:
                db.execute('DELETE FROM agent_watchdog_restarts WHERE created<?', (time.time() - 86400,))
                if db.execute('SELECT COUNT(*) FROM agent_watchdog_restarts WHERE created>?',
                              (time.time() - 3600,)).fetchone()[0] >= 2:
                    return False
                db.execute('INSERT INTO agent_watchdog_restarts(created,pid,started) VALUES(?,?,?)',
                           (time.time(), process.pid if isinstance(process.pid, int) else 0, started))
            journal(queue.store, 'restart_reserved')
            parent = process.parent()
            redirector = parent if parent is not None and identity(parent, home) else None
            process.terminate()
            try:
                process.wait(5)
            except psutil.TimeoutExpired:
                if process.create_time() != started:
                    raise RuntimeError('Backend process identity changed')
                process.kill()
                process.wait(5)
            if redirector is not None:
                redirector.wait(5)  # Let the verified venv wrapper exit before spawning another one.
            runner.spawn_script(runner.http)
            runner.local_mcp()
            journal(queue.store, 'restart_verified')
            return True
        finally:
            sessions.release_operation(lease)


def main(home, pid, started, worker):
    home = Path(home).resolve(strict=True)
    queue = AgentQueue(home / 'config/missions.sqlite3')
    journal(queue.store, 'watchdog_started')
    previous = None
    stale_since = None
    while queue.store.alive(pid, started):
        try:
            policy = json.loads((home / 'config/config.json').read_text())
            observed = health(queue.store, worker)
            if observed['state'] == 'stopped':
                return
            enabled = (policy.get('agents', {}).get('enabled') and policy.get('missions', {}).get('enabled')
                       and policy.get('agents', {}).get('watchdog_enabled', True))
            if observed['state'] == 'stalled' and enabled:
                stale_since = stale_since if stale_since is not None else time.monotonic()
            else:
                stale_since = None
            if stale_since is not None and time.monotonic() - stale_since >= 6:
                if attempt_restart(home, psutil.Process(pid), started, worker, queue):
                    return
        except Exception as exc:
            code = 'restart_deferred_' + type(exc).__name__
            if code != previous:
                journal(queue.store, code)
                previous = code
        time.sleep(3)


if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4])
