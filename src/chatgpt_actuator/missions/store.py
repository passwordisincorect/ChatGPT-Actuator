"""Transactional mission records and append-only attempt/checkpoint history."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

import psutil


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class MissionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mission_meta(version INTEGER NOT NULL);
                INSERT INTO mission_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM mission_meta);
                CREATE TABLE IF NOT EXISTS missions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL, updated REAL NOT NULL,
                    UNIQUE(owner, request_key));
                CREATE TABLE IF NOT EXISTS mission_attempts(
                    id TEXT PRIMARY KEY, mission TEXT NOT NULL, step INTEGER,
                    pid INTEGER NOT NULL, started REAL NOT NULL, status TEXT NOT NULL,
                    dispatched INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    finished REAL, evidence TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_mission_attempt
                    ON mission_attempts(mission) WHERE status='RUNNING';
                CREATE UNIQUE INDEX IF NOT EXISTS one_unsettled_mission_attempt
                    ON mission_attempts(mission) WHERE status IN ('RUNNING','CLEANUP_PENDING');
                CREATE TABLE IF NOT EXISTS mission_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, mission TEXT NOT NULL,
                    kind TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mission_checkpoints(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, mission TEXT NOT NULL,
                    created REAL NOT NULL, document TEXT NOT NULL);
            """)
            if db.execute('SELECT version FROM mission_meta').fetchall() != [(1,)]:
                raise RuntimeError('Unsupported mission schema')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def alive(pid, started):
        try:
            return psutil.Process(pid).create_time() == started
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except (OSError, psutil.AccessDenied):
            return True

    def load(self, db, owner, mission_id):
        row = db.execute('SELECT owner,document FROM missions WHERE id=?', (mission_id,)).fetchone()
        if not row or row[0] != owner:
            raise PermissionError('Unknown or foreign mission')
        doc = json.loads(row[1])
        active = db.execute("SELECT id,step,pid,started,dispatched,created FROM mission_attempts "
                            "WHERE mission=? AND status IN ('RUNNING','CLEANUP_PENDING')", (mission_id,)).fetchone()
        if active and not self.alive(active[2], active[3]):
            attempt, step, _, _, dispatched, created = active
            db.execute("UPDATE mission_attempts SET status='INTERRUPTED',finished=? WHERE id=?",
                       (time.time(), attempt))
            doc['state'] = 'BLOCKED'
            doc['reason'] = 'runtime_interrupted'
            doc['elapsed_seconds'] += min(30.0, max(0.0, time.time() - created))
            if step is not None:
                doc['steps'][step]['state'] = 'UNKNOWN' if dispatched else 'PENDING'
            self.save(db, doc, 'recovered', {'attempt_id': attempt, 'delivery_unknown': bool(dispatched)})
        return doc

    def save(self, db, doc, kind, detail=None):
        now = time.time()
        doc['updated'] = now
        payload = encode(doc)
        db.execute('UPDATE missions SET document=?,updated=? WHERE id=?', (payload, now, doc['id']))
        db.execute('INSERT INTO mission_events(mission,kind,created,detail) VALUES(?,?,?,?)',
                   (doc['id'], kind, now, encode(detail or {})))
        db.execute('INSERT INTO mission_checkpoints(mission,created,document) VALUES(?,?,?)',
                   (doc['id'], now, payload))

    def claim(self, db, doc, step):
        for owner, mission_id in db.execute("SELECT DISTINCT m.owner,m.id FROM missions m "
                "JOIN mission_attempts a ON a.mission=m.id WHERE a.status IN "
                "('RUNNING','CLEANUP_PENDING')").fetchall():
            self.load(db, owner, mission_id)
        if db.execute("SELECT 1 FROM mission_attempts WHERE mission=? AND status IN "
                      "('RUNNING','CLEANUP_PENDING')", (doc['id'],)).fetchone():
            raise RuntimeError('Mission has an unsettled provider attempt')
        active = db.execute("SELECT COUNT(*) FROM mission_attempts WHERE status IN "
                            "('RUNNING','CLEANUP_PENDING')").fetchone()[0]
        if active >= 4:
            raise RuntimeError('Mission concurrency limit reached')
        attempt = secrets.token_urlsafe(24)
        db.execute('INSERT INTO mission_attempts(id,mission,step,pid,started,status,created) '
                   'VALUES(?,?,?,?,?,?,?)', (attempt, doc['id'], step, os.getpid(),
                   psutil.Process().create_time(), 'RUNNING', time.time()))
        doc['state'] = 'RUNNING'
        self.save(db, doc, 'attempt_started', {'attempt_id': attempt, 'step': step})
        return attempt

    def finish(self, db, doc, attempt, evidence, cleanup_pending=False):
        db.execute("UPDATE mission_attempts SET status=?,finished=?,evidence=? WHERE id=?",
                   ('CLEANUP_PENDING' if cleanup_pending else 'FINISHED', time.time(), encode(evidence), attempt))
        self.save(db, doc, 'attempt_finished', {'attempt_id': attempt, 'state': doc['state']})
