"""Durable scheduling and worker observations. Expiry alone never transfers a claim."""
import json
import os
from pathlib import Path
import time

import psutil
from . import clocks


class StaleClaim(RuntimeError):
    pass


def overlaps(left, right):
    a, b = Path(left).resolve(), Path(right).resolve()
    return a == b or a in b.parents or b in a.parents


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_schedule(
            job TEXT PRIMARY KEY, priority INTEGER NOT NULL, last_served REAL NOT NULL,
            eligible REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_workspace_locks(
            job TEXT PRIMARY KEY, workspace TEXT NOT NULL, resource TEXT);
        CREATE TABLE IF NOT EXISTS agent_workers(
            id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started REAL NOT NULL,
            heartbeat REAL NOT NULL, stopped INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS agent_jobs_ready_idx ON agent_jobs(state,due);
        CREATE TABLE IF NOT EXISTS agent_worker_clocks(id TEXT PRIMARY KEY,document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_worker_capabilities(
            worker TEXT PRIMARY KEY,schema INTEGER NOT NULL,pid INTEGER NOT NULL,started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_execution_policy(id INTEGER PRIMARY KEY,required_schema INTEGER NOT NULL);
        INSERT OR IGNORE INTO agent_execution_policy VALUES(1,33);
        CREATE TRIGGER IF NOT EXISTS agent_contract_worker_guard
        BEFORE UPDATE OF state,document ON agent_jobs
        WHEN NEW.state IN ('RUNNING','PAUSING','CANCELLING')
          AND (SELECT required_schema FROM agent_execution_policy WHERE id=1)>=33
          AND NOT EXISTS(SELECT 1 FROM agent_worker_capabilities c
              WHERE c.worker=json_extract(NEW.document,'$.worker_id')
                AND c.schema>=33 AND c.pid=NEW.pid AND c.started=NEW.started)
        BEGIN SELECT RAISE(ABORT,'Worker cannot execute the frozen V3.3 contract'); END;
        INSERT OR IGNORE INTO agent_schedule SELECT id,0,0,0 FROM agent_jobs;
    ''')


def resource(doc):
    recovery = doc.get('browser_recovery')
    if recovery:
        return 'target:' + recovery['cdp_url'] + ':' + recovery['tab_id']
    if doc.get('delegated_session'):
        return 'session:' + doc['owner'] + ':' + doc['delegated_session']
    return None


def ready(db, now):
    rows = db.execute("SELECT j.document,s.priority,s.last_served FROM agent_jobs j "
        "JOIN agent_schedule s ON s.job=j.id WHERE j.state='QUEUED' AND j.due<=? AND s.eligible<=?",
        (now, now)).fetchall()
    locks = db.execute('SELECT job,workspace,resource FROM agent_workspace_locks').fetchall()
    ranked = []
    for raw, priority, last in rows:
        doc = json.loads(raw)
        if not clocks.ready(doc.get('_schedule_clock'), doc['run_at']):
            continue
        if doc.get('retry_at') and not clocks.ready(doc.get('_retry_clock'), doc['retry_at']):
            continue
        if any(job != doc['id'] and (overlaps(root, doc['workspace']) or
               (binding is not None and binding == resource(doc))) for job, root, binding in locks):
            continue
        age = max(0, now - (last or doc['created']))
        effective = min(3, priority + int(age // 300))
        ranked.append((-effective, last, doc['created'], doc['id'], doc))
    ranked.sort(key=lambda row: row[:4])
    return [row[-1] for row in ranked]


def release_if_settled(db, doc):
    if doc['state'] not in {'COMPLETED', 'CANCELLED', 'FAILED', 'BLOCKED', 'PAUSED'}:
        return
    mid = doc.get('mission_id')
    if mid:
        row = db.execute('SELECT document FROM missions WHERE id=?', (mid,)).fetchone()
        if row and any(s['state'] == 'UNKNOWN' for s in json.loads(row[0])['steps']):
            return
        if db.execute("SELECT 1 FROM mission_attempts WHERE mission=? AND status IN ('RUNNING','CLEANUP_PENDING')",
                      (mid,)).fetchone():
            return
    db.execute('DELETE FROM agent_workspace_locks WHERE job=?', (doc['id'],))


def beat(store, worker, stopped=False):
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO agent_workers VALUES(?,?,?,?,?)',
                   (worker, os.getpid(), psutil.Process().create_time(), time.time(), int(stopped)))
        db.execute('INSERT OR REPLACE INTO agent_worker_clocks VALUES(?,?)',
                   (worker, json.dumps(clocks.point())))
        db.execute('DELETE FROM agent_workers WHERE stopped=1 AND heartbeat<?', (time.time() - 86400,))
        db.execute("DELETE FROM agent_workers WHERE heartbeat<? AND id NOT IN "
            "(SELECT COALESCE(json_extract(document,'$.worker_id'),'') FROM agent_jobs "
            "WHERE state IN ('RUNNING','PAUSING','CANCELLING'))", (time.time() - 7 * 86400,))
        db.execute('DELETE FROM agent_worker_clocks WHERE id NOT IN (SELECT id FROM agent_workers)')
        db.execute("DELETE FROM agent_worker_capabilities WHERE worker NOT IN (SELECT id FROM agent_workers) "
            "AND worker NOT IN (SELECT COALESCE(json_extract(document,'$.worker_id'),'') FROM agent_jobs "
            "WHERE state IN ('RUNNING','PAUSING','CANCELLING'))")


def health(store, worker=None):
    with store.db() as db:
        row = db.execute('SELECT id,pid,started,heartbeat,stopped FROM agent_workers WHERE id=?',
                         (worker,)).fetchone() if worker else db.execute(
                         'SELECT id,pid,started,heartbeat,stopped FROM agent_workers ORDER BY heartbeat DESC LIMIT 1').fetchone()
        clock = db.execute('SELECT document FROM agent_worker_clocks WHERE id=?', (row[0],)).fetchone() if row else None
    if not row:
        return {'state': 'not_started', 'stale': False}
    age = max(0, time.time() - row[3])
    if clock:
        old, now = json.loads(clock[0]), clocks.point()
        if old['boot'] == now['boot'] and now['tick'] >= old['tick']:
            age = now['tick'] - old['tick']
    alive = store.alive(row[1], row[2])
    return {'worker_id': row[0], 'pid': row[1], 'started': row[2], 'heartbeat': row[3],
            'age_seconds': age, 'stale': age > 90,
            'state': 'stopped' if row[4] else 'dead' if not alive else 'stalled' if age > 90 else 'healthy'}
