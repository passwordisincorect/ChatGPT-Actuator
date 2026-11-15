"""Bounded compressed history with permanent owner/key/digest tombstones."""
import gzip
import io
import json
import time

from ..missions.store import encode

TERMINAL = {'COMPLETED', 'CANCELLED', 'FAILED'}
MAX_RAW = 4 * 1024 * 1024


class JobArchive:
    def __init__(self, queue):
        self.queue, self.store = queue, queue.store
        with self.store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS agent_archives(
                job TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
                payload BLOB, raw_size INTEGER NOT NULL)''')

    def _archive(self, db, owner, job_id):
        doc = self.queue.get(db, owner, job_id)
        if doc.get('archived'):
            return self.queue.summary(doc)
        if doc['state'] not in TERMINAL:
            raise ValueError('Only terminal jobs may be archived')
        prefixes = ['agent-' + job_id + '-', 'recovery-' + job_id + '-']
        missions = []
        for prefix in prefixes:
            missions.extend(db.execute('SELECT id,document FROM missions WHERE owner=? '
                'AND substr(request_key,1,?)=?', (owner, len(prefix), prefix)).fetchall())
        if doc.get('mission_id') and not any(mid == doc['mission_id'] for mid, _ in missions):
            row = db.execute('SELECT id,document FROM missions WHERE id=? AND owner=?',
                             (doc['mission_id'], owner)).fetchone()
            if row is None:
                raise ValueError('Unresolved linked Mission')
            missions.append(row)
        bundle = {'job': doc, 'events': db.execute('SELECT * FROM agent_events WHERE job=? ORDER BY seq',
                                                 (job_id,)).fetchall(), 'missions': []}
        for mid, raw in missions:
            mission = json.loads(raw)
            if mission.get('archived'):
                continue
            if any(s['state'] == 'UNKNOWN' for s in mission['steps']):
                raise ValueError('Cannot archive a job with an unresolved Mission')
            if db.execute("SELECT 1 FROM mission_attempts WHERE mission=? AND status IN ('RUNNING','CLEANUP_PENDING')",
                          (mid,)).fetchone():
                raise ValueError('Provider cleanup must settle before archival')
            if mission['state'] not in TERMINAL:
                if mission['state'] not in {'PENDING', 'PAUSED', 'BLOCKED'}:
                    raise ValueError('Mission has not settled')
                mission['state'], mission['reason'] = 'CANCELLED', 'parent_archived_after_settlement'
            bundle['missions'].append({'document': mission,
                'attempts': db.execute('SELECT * FROM mission_attempts WHERE mission=?', (mid,)).fetchall(),
                'events': db.execute('SELECT * FROM mission_events WHERE mission=? ORDER BY seq', (mid,)).fetchall(),
                'checkpoints': db.execute('SELECT * FROM mission_checkpoints WHERE mission=? ORDER BY seq', (mid,)).fetchall()})
        raw = encode(bundle).encode('utf-8')
        if len(raw) > MAX_RAW:
            raise RuntimeError('Archive exceeds the bounded history size; export it locally before retention changes')
        payload = gzip.compress(raw, mtime=0)
        if len(payload) > 1048576:
            raise RuntimeError('Compressed archive size exceeded')
        db.execute('UPDATE agent_archives SET payload=NULL WHERE created<?', (time.time() - 90 * 86400,))
        total = db.execute('SELECT COALESCE(SUM(length(payload)),0) FROM agent_archives').fetchone()[0]
        own = db.execute('SELECT COALESCE(SUM(length(payload)),0) FROM agent_archives WHERE owner=?', (owner,)).fetchone()[0]
        if total + len(payload) > 64 * 1048576 or own + len(payload) > 16 * 1048576:
            raise RuntimeError('Archive storage budget reached; idempotency keys must not be deleted')
        db.execute('INSERT INTO agent_archives VALUES(?,?,?,?,?)', (job_id, owner, time.time(), payload, len(raw)))
        for item in bundle['missions']:
            mission = item['document']
            compact = {k: mission[k] for k in ('id', 'owner', 'goal', 'digest', 'state', 'reason',
                'created', 'max_seconds', 'max_attempts', 'elapsed_seconds')}
            compact.update(steps=[], criteria=[], archived=True, archive_job=job_id)
            db.execute('UPDATE missions SET document=? WHERE id=?', (encode(compact), mission['id']))
            for table in ('mission_attempts', 'mission_events', 'mission_checkpoints'):
                db.execute('DELETE FROM ' + table + ' WHERE mission=?', (mission['id'],))
        compact = self.queue.summary(doc)
        compact.update(owner=owner, archived=True, archived_at=time.time(), archive_details_available=True)
        db.execute('UPDATE agent_jobs SET document=? WHERE id=?', (encode(compact), job_id))
        db.execute('DELETE FROM agent_events WHERE job=?', (job_id,))
        db.execute('DELETE FROM agent_workspace_locks WHERE job=?', (job_id,))
        return compact

    def archive(self, owner, job_id, expected_updated=None):
        with self.store.db() as db:
            doc = self.queue.get(db, owner, job_id)
            if expected_updated is not None and doc.get('updated') != expected_updated:
                raise ValueError('Job changed; refresh before archiving')
            return self._archive(db, owner, job_id)

    def make_room(self, db, owner):
        # Auto-retire only terminal jobs when admission would otherwise fail.
        count = db.execute('SELECT COUNT(*) FROM agent_jobs j WHERE owner=? AND NOT EXISTS '
            '(SELECT 1 FROM agent_archives a WHERE a.job=j.id)', (owner,)).fetchone()[0]
        global_count = db.execute('SELECT COUNT(*) FROM agent_jobs j WHERE NOT EXISTS '
            '(SELECT 1 FROM agent_archives a WHERE a.job=j.id)').fetchone()[0]
        if count < 100 and global_count < 1000:
            return
        rows = db.execute("SELECT id FROM agent_jobs j WHERE owner=? AND state IN ('COMPLETED','CANCELLED','FAILED') "
            'AND NOT EXISTS (SELECT 1 FROM agent_archives a WHERE a.job=j.id) ORDER BY rowid LIMIT 25', (owner,)).fetchall()
        for row in rows:
            try:
                self._archive(db, owner, row[0])
            except (ValueError, RuntimeError):
                continue
            break

    def read(self, db, owner, job_id):
        self.queue.get(db, owner, job_id)
        row = db.execute('SELECT payload,raw_size FROM agent_archives WHERE job=? AND owner=?', (job_id, owner)).fetchone()
        if not row or row[0] is None:
            return {'job': self.queue.summary(self.queue.get(db, owner, job_id)),
                    'archive_details_available': False, 'idempotency_key_retained': True}
        with gzip.GzipFile(fileobj=io.BytesIO(row[0])) as stream:
            raw = stream.read(MAX_RAW + 1)
        if len(raw) > MAX_RAW or len(raw) != row[1]:
            raise RuntimeError('Archive data failed its size contract')
        return {**json.loads(raw), 'archived': True, 'idempotency_key_retained': True}
