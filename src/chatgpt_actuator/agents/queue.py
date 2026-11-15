"""Owner-scoped scheduled jobs, crash recovery and conservative usage reservations."""
import hashlib
import json
import os
import re
import secrets
import time

import psutil

from ..missions.store import MissionStore, encode
from . import coordination, clocks


class AgentQueue:
    def __init__(self, path):
        self.store = MissionStore(path)
        with self.store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_jobs(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL, due REAL NOT NULL,
                    pid INTEGER, started REAL, document TEXT NOT NULL,
                    UNIQUE(owner,request_key));
                CREATE TABLE IF NOT EXISTS agent_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                    kind TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_usage(
                    owner TEXT NOT NULL, day INTEGER NOT NULL, reserved INTEGER NOT NULL,
                    PRIMARY KEY(owner,day));
                CREATE TABLE IF NOT EXISTS agent_rechecks(job TEXT PRIMARY KEY,tries INTEGER,last_at REAL);
            ''')
            coordination.initialize(db)
        from .archive import JobArchive
        self.archive = JobArchive(self)

    def save(self, db, doc, kind, preserve_control=True, claiming=False):
        row = db.execute('SELECT document FROM agent_jobs WHERE id=?', (doc['id'],)).fetchone()
        current = json.loads(row[0]) if row else {}
        if not claiming and current.get('_claim_token') != doc.get('_claim_token'):
            raise coordination.StaleClaim('Worker claim has been superseded')
        if preserve_control:
            row = db.execute('SELECT document FROM agent_jobs WHERE id=?', (doc['id'],)).fetchone()
            request = json.loads(row[0]).get('requested_action') if row else None
            if request and not doc.get('requested_action'):
                doc['requested_action'] = request
                if doc['state'] == 'RUNNING':
                    doc['state'] = 'PAUSING' if request == 'pause' else 'CANCELLING'
        if not doc.get('archived'):
            clocks.advance(doc)
        doc['updated'] = time.time()
        db.execute('UPDATE agent_jobs SET state=?,document=? WHERE id=?',
                   (doc['state'], encode(doc), doc['id']))
        db.execute('INSERT INTO agent_events(job,kind,created,detail) VALUES(?,?,?,?)',
                   (doc['id'], kind, doc['updated'], encode({'state': doc['state'], 'reason': doc.get('reason')})))
        coordination.release_if_settled(db, doc)

    def get(self, db, owner, job_id):
        row = db.execute('SELECT owner,document FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row[0] != owner:
            raise PermissionError('Unknown or foreign agent job')
        return json.loads(row[1])

    def submit(self, owner, request_key, plan):
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', request_key):
            raise ValueError('Provide a stable request_key')
        digest = hashlib.sha256(encode(plan).encode()).hexdigest()
        with self.store.db() as db:
            row = db.execute('SELECT id,digest FROM agent_jobs WHERE owner=? AND request_key=?',
                             (owner, request_key)).fetchone()
            if row:
                if row[1] != digest:
                    raise ValueError('Agent idempotency key conflicts with another task')
                return self.get(db, owner, row[0])
            self.archive.make_room(db, owner)
            if db.execute('SELECT COUNT(*) FROM agent_jobs j WHERE owner=? AND NOT EXISTS '
                    '(SELECT 1 FROM agent_archives a WHERE a.job=j.id)', (owner,)).fetchone()[0] >= 100:
                raise RuntimeError('Owner active job limit reached')
            if db.execute('SELECT COUNT(*) FROM agent_jobs j WHERE NOT EXISTS '
                    '(SELECT 1 FROM agent_archives a WHERE a.job=j.id)').fetchone()[0] >= 1000:
                raise RuntimeError('Global job storage limit reached')
            if db.execute('SELECT COUNT(*) FROM agent_jobs').fetchone()[0] >= 20000:
                raise RuntimeError('Retained idempotency key storage limit reached')
            doc = {**plan, 'id': secrets.token_urlsafe(24), 'owner': owner, 'state': 'QUEUED',
                   'reason': None, 'created': time.time(), 'started_at': None,
                   'planner_calls': 0, 'reserved_tokens': 0, 'reported_tokens': 0,
                   'cycles': 0, 'session_id': None, 'mission_id': None, 'decision': None,
                   'memory': [], 'seen': [], 'requested_action': None}
            if plan['run_at'] > time.time():
                doc['_schedule_clock'] = clocks.timer(plan['run_at'])
            db.execute('INSERT INTO agent_jobs VALUES(?,?,?,?,?,?,NULL,NULL,?)',
                       (doc['id'], owner, request_key, digest, doc['state'], plan['run_at'], encode(doc)))
            db.execute('INSERT INTO agent_schedule VALUES(?,?,0,0)', (doc['id'], plan.get('priority', 0)))
            self.save(db, doc, 'submitted')
            return doc

    def recover(self, db):
        for job_id, owner, pid, started in db.execute(
                "SELECT id,owner,pid,started FROM agent_jobs WHERE state IN ('RUNNING','PAUSING','CANCELLING')").fetchall():
            doc = self.get(db, owner, job_id)
            worker = db.execute('SELECT stopped FROM agent_workers WHERE id=?', (doc.get('worker_id'),)).fetchone()
            if pid is not None and self.store.alive(pid, started) and not (worker and worker[0]):
                continue  # An old heartbeat is a diagnostic, never proof of safe takeover.
            request = doc.get('requested_action')
            doc['state'] = 'CANCELLED' if request == 'cancel' else 'PAUSED' if request == 'pause' else 'QUEUED'
            doc['reason'] = 'worker_interrupted'
            doc['worker_recoveries'] = doc.get('worker_recoveries', 0) + 1
            doc['session_id'] = None  # References from the old runtime cannot be silently reused.
            self.save(db, doc, 'recovered')

    def claim(self, worker_id=None):
        with self.store.db() as db:
            self.recover(db)
            for raw, in db.execute("SELECT document FROM agent_jobs WHERE state='QUEUED'").fetchall():
                pending = json.loads(raw)
                if any(pending.get(key) and pending[key]['boot'] != clocks.boot_id()
                       for key in ('_schedule_clock', '_retry_clock')):
                    pending['state'], pending['reason'] = 'PAUSED', 'schedule_clock_rebase_required'
                    self.save(db, pending, 'schedule_clock_unverified')
            if db.execute("SELECT 1 FROM agent_jobs WHERE state IN ('RUNNING','PAUSING','CANCELLING')").fetchone():
                return None
            choices = coordination.ready(db, time.time())
            if not choices:
                return None
            doc = choices[0]
            worker_id = worker_id or secrets.token_urlsafe(24)
            db.execute('INSERT OR REPLACE INTO agent_worker_capabilities VALUES(?,?,?,?)',
                       (worker_id,33,os.getpid(),psutil.Process().create_time()))
            doc['state'], doc['reason'] = 'RUNNING', None
            doc['started_at'] = doc['started_at'] or time.time()
            doc['_claim_token'] = secrets.token_urlsafe(24)
            doc['worker_id'] = worker_id
            doc['first_started_at'] = doc.get('first_started_at') or time.time()
            doc['claim_count'] = doc.get('claim_count', 0) + 1
            db.execute('UPDATE agent_schedule SET last_served=?,eligible=0 WHERE job=?', (time.time(), doc['id']))
            db.execute('INSERT OR REPLACE INTO agent_workspace_locks VALUES(?,?,?)',
                       (doc['id'], doc['workspace'], coordination.resource(doc)))
            db.execute('UPDATE agent_jobs SET pid=?,started=? WHERE id=?',
                       (os.getpid(), psutil.Process().create_time(), doc['id']))
            self.save(db, doc, 'claimed', claiming=True)
            return doc

    def assert_claim(self, doc):
        with self.store.db() as db:
            current = self.get(db, doc['owner'], doc['id'])
        if current.get('_claim_token') != doc.get('_claim_token') or current['state'] not in {'RUNNING', 'PAUSING', 'CANCELLING'}:
            raise coordination.StaleClaim('Worker no longer owns the running task')
        return current

    def yield_job(self, doc, reason='fair_yield', delay=0):
        self.assert_claim(doc)
        doc['state'], doc['reason'] = 'QUEUED', reason
        doc['yield_count'] = doc.get('yield_count', 0) + (reason == 'fair_yield')
        doc['retry_at'] = time.time() + delay if delay else None
        doc['_retry_clock'] = clocks.timer(doc['retry_at']) if delay else None
        with self.store.db() as db:
            db.execute('UPDATE agent_schedule SET eligible=? WHERE job=?', (doc['retry_at'] or 0, doc['id']))
            self.save(db, doc, reason)

    def has_competitor(self, doc):
        with self.store.db() as db:
            return any(other['id'] != doc['id'] for other in coordination.ready(db, time.time()))

    def wait_inputs(self, doc):
        self.yield_job(doc,'waiting_inputs',30)
        with self.store.db() as db:
            # Only called at a settled checkpoint with no pending decision/Mission.
            db.execute('DELETE FROM agent_workspace_locks WHERE job=?',(doc['id'],))

    def signal_inputs(self, root):
        with self.store.db() as db:
            rows=db.execute("SELECT document FROM agent_jobs WHERE state='QUEUED'").fetchall()
            for raw, in rows:
                doc=json.loads(raw)
                if doc.get('reason')=='waiting_inputs' and doc['workspace']==root and not doc.get('requested_action'):
                    doc['retry_at'],doc['_retry_clock']=None,None
                    doc['input_signals']=doc.get('input_signals',0)+1
                    db.execute('UPDATE agent_schedule SET eligible=0 WHERE job=?',(doc['id'],))
                    self.save(db,doc,'input_change_signal')

    def reserve(self, doc, amount=30000):
        with self.store.db() as db:
            current = self.get(db, doc['owner'], doc['id'])
            if current.get('_claim_token') != doc.get('_claim_token') or current['state'] != 'RUNNING':
                if current.get('requested_action'):
                    return False
                raise coordination.StaleClaim('Planner reservation requires the current claim')
            if current.get('requested_action'):
                return False
            if doc['planner_calls'] >= doc['max_calls'] or doc['reserved_tokens'] + amount > doc['token_budget']:
                raise RuntimeError('Planner budget exhausted')
            day = clocks.accounting_day(db)
            db.execute('INSERT OR IGNORE INTO agent_usage VALUES(?,?,0)', (doc['owner'], day))
            used = db.execute('SELECT reserved FROM agent_usage WHERE owner=? AND day=?', (doc['owner'], day)).fetchone()[0]
            if used + amount > 1000000:
                raise RuntimeError('Owner daily planner budget exhausted')
            global_used = db.execute('SELECT COALESCE(SUM(reserved),0) FROM agent_usage WHERE day=?', (day,)).fetchone()[0]
            if global_used + amount > 2000000:
                raise RuntimeError('Global daily planner budget exhausted')
            db.execute('UPDATE agent_usage SET reserved=reserved+? WHERE owner=? AND day=?',
                       (amount, doc['owner'], day))
            doc['planner_calls'] += 1
            doc['reserved_tokens'] += amount
            self.save(db, doc, 'planner_reserved')
            return True

    def status(self, owner, job_id=None, include_archived=False):
        with self.store.db() as db:
            self.recover(db)
            if job_id is None:
                rows = db.execute('SELECT document FROM agent_jobs WHERE owner=? ORDER BY rowid DESC LIMIT 50', (owner,)).fetchall()
                return {'jobs': [self.summary(json.loads(r[0])) for r in rows]}
            doc = self.get(db, owner, job_id)
            if doc.get('archived') and include_archived:
                return self.archive.read(db, owner, job_id)
            if doc.get('archived'):
                row = db.execute('SELECT payload IS NOT NULL FROM agent_archives WHERE job=? AND owner=?',
                                 (job_id, owner)).fetchone()
                doc['archive_details_available'] = bool(row and row[0])
            rows = db.execute('SELECT seq,kind,created,detail FROM agent_events WHERE job=? '
                              'ORDER BY seq DESC LIMIT 50', (job_id,)).fetchall()
            return {**doc, 'recovery':self.recovery_summary(doc), 'events': [{'seq': r[0], 'kind': r[1], 'created': r[2],
                                      'detail': json.loads(r[3])} for r in reversed(rows)]}

    @staticmethod
    def recovery_summary(doc):
        return {'next_action': 'inspect_evidence' if doc['state']=='BLOCKED' else
                'resume' if doc['state']=='PAUSED' else 'none' if doc['state'] in {'COMPLETED','FAILED','CANCELLED'} else 'wait',
                'verified_milestones':doc.get('milestone_index',0),
                'verified_graph_nodes':[key for key,value in doc.get('graph_states',{}).items() if value.get('state')=='DONE']}

    @staticmethod
    def summary(doc):
        return {**{k: doc.get(k) for k in ('id', 'goal', 'state', 'reason', 'created', 'updated', 'run_at',
            'planner_calls', 'reserved_tokens', 'reported_tokens', 'cycles', 'mission_id', 'requested_action')},
            'recovery':AgentQueue.recovery_summary(doc),
            'milestone_index': doc.get('milestone_index', 0),
            'milestone_count': len(doc.get('milestones', [])),
            'milestones': [{'title': p['title'], 'verified': i < doc.get('milestone_index', 0)}
                           for i, p in enumerate(doc.get('milestones', []))],
            'max_calls': doc.get('max_calls'), 'max_cycles': doc.get('max_cycles'),
            'max_seconds': doc.get('max_seconds'), 'token_budget': doc.get('token_budget'),
            'recovery_count': doc.get('recovery_count', 0), 'archived': doc.get('archived', False),
            'priority': doc.get('priority', 0), 'retry_count': doc.get('retry_count', 0),
            'retry_at': doc.get('retry_at'), 'yield_count': doc.get('yield_count', 0),
            'first_started_at': doc.get('first_started_at'), 'quality_evidence': doc.get('quality_evidence', []),
            'planner_seconds': doc.get('planner_seconds', 0), 'planner_failures': doc.get('planner_failures', 0),
            'worker_recoveries': doc.get('worker_recoveries', 0), 'quality_failures': doc.get('quality_failures', 0),
            'prompt_bytes': doc.get('prompt_bytes', 0), 'clock_issue': doc.get('clock_issue'),
            'recheck_count': doc.get('recheck_count', 0), 'planner_failures': doc.get('planner_failures', 0),
            'recipe_kind': doc.get('recipe',{}).get('kind'), 'recipe_steps': doc.get('recipe_steps',0),
            'input_checks':doc.get('input_checks',0),'input_signals':doc.get('input_signals',0),
            'waiting_for_inputs':doc.get('reason')=='waiting_inputs',
            'graph_nodes':[{'id':n['id'],'title':n['title'],'needs':n['needs'],
                'state':doc.get('graph_states',{}).get(n['id'],{}).get('state','PENDING')} for n in doc.get('graph',[])],
            'elapsed_seconds': doc.get('_duration_clock', {}).get('elapsed', doc.get('elapsed_seconds')),
            'blockers': ([{'tool': e.get('tool'), 'error_type': e.get('error_type'), 'path': e.get('path')}
                          for e in doc.get('goal_evidence', []) if not e.get('passed')][:10] +
                         [{'validator': e.get('kind'), 'path': e.get('path'), 'issues': e.get('issues', [])[:5]}
                          for e in doc.get('quality_evidence', []) if not e.get('passed')][:10])}

    def control(self, owner, job_id, action, expected_updated=None):
        if action not in {'pause', 'resume', 'cancel'}:
            raise ValueError('Choose pause, resume or cancel')
        with self.store.db() as db:
            self.recover(db)
            doc = self.get(db, owner, job_id)
            if expected_updated is not None and (type(expected_updated) not in (int, float)
                                                or doc.get('updated') != expected_updated):
                raise ValueError('Job changed; refresh before controlling it')
            if doc['state'] in {'COMPLETED', 'CANCELLED', 'FAILED'}:
                raise ValueError('Job is terminal')
            if doc['state'] in {'RUNNING', 'PAUSING', 'CANCELLING'}:
                if action == 'resume':
                    raise ValueError('Wait for the active operation to settle')
                doc['requested_action'] = action
                doc['state'] = 'PAUSING' if action == 'pause' else 'CANCELLING'
            elif action == 'resume':
                for key, deadline in (('_schedule_clock', doc['run_at']), ('_retry_clock', doc.get('retry_at'))):
                    if deadline and doc.get(key) and doc[key]['boot'] != clocks.boot_id():
                        doc[key] = clocks.timer(deadline)
                doc['requested_action'] = None
                doc['state'], doc['reason'] = 'QUEUED', None
            else:
                doc['state'] = 'PAUSED' if action == 'pause' else 'CANCELLED'
            self.save(db, doc, action, preserve_control=False)
            return self.summary(doc)
