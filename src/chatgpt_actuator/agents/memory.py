"""Private expiring project hints with provenance, never authority or fresh evidence."""
import hashlib
import json
from pathlib import Path
import re
import time

from ..missions.store import encode


class ProjectMemory:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS agent_memory(
                owner TEXT NOT NULL, workspace TEXT NOT NULL, key TEXT NOT NULL,
                expires REAL NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(owner,workspace,key))''')

    def put(self, owner, workspace, key, note, source, ttl_days=30):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', key):
            raise ValueError('Bounded memory key required')
        if not isinstance(note, str) or not 1 <= len(note.encode('utf-8')) <= 2000:
            raise ValueError('Memory note must be 1..2000 bytes')
        if type(ttl_days) is not int or not 1 <= ttl_days <= 90:
            raise ValueError('Memory lifetime must be 1..90 days')
        workspace = str(Path(workspace).resolve())
        now = time.time()
        doc = {'key': key, 'note': note, 'source': source, 'created': now,
               'expires': now + ttl_days * 86400, 'historical_hint_only': True}
        with self.store.db() as db:
            db.execute('DELETE FROM agent_memory WHERE expires<=?', (now,))
            existing = db.execute('SELECT 1 FROM agent_memory WHERE owner=? AND workspace=? AND key=?',
                                  (owner, workspace, key)).fetchone()
            if not existing and db.execute('SELECT COUNT(*) FROM agent_memory WHERE owner=?', (owner,)).fetchone()[0] >= 1000:
                raise RuntimeError('Owner project memory limit reached')
            if not existing and db.execute('SELECT COUNT(*) FROM agent_memory WHERE owner=? AND workspace=?',
                                          (owner, workspace)).fetchone()[0] >= 100:
                raise RuntimeError('Project memory limit reached')
            if not existing and db.execute('SELECT COUNT(*) FROM agent_memory').fetchone()[0] >= 10000:
                raise RuntimeError('Global project memory limit reached')
            db.execute('INSERT OR REPLACE INTO agent_memory VALUES(?,?,?,?,?)',
                       (owner, workspace, key, doc['expires'], encode(doc)))
        return doc

    def list(self, owner, workspace):
        with self.store.db() as db:
            rows = db.execute('SELECT document FROM agent_memory WHERE owner=? AND workspace=? AND expires>? '
                              'ORDER BY expires DESC,key LIMIT 8',
                              (owner, str(Path(workspace).resolve()), time.time())).fetchall()
        result, size = [], 0
        for row in rows:
            size += len(row[0].encode('utf-8'))
            if size > 8000:
                break
            result.append(json.loads(row[0]))
        return {'memories': result, 'historical_hint_only': True}

    def forget(self, owner, workspace, key):
        if not isinstance(key, str) or not key:
            raise ValueError('Memory key required')
        with self.store.db() as db:
            cursor = db.execute('DELETE FROM agent_memory WHERE owner=? AND workspace=? AND key=?',
                                (owner, str(Path(workspace).resolve()), key))
        return {'forgotten': bool(cursor.rowcount)}

    def remember_job(self, doc):
        # Keep provenance and hashes, not raw file contents or tool responses.
        source = {'kind': 'verified_job', 'job_id': doc['id'],
                  'criteria_sha256': hashlib.sha256(encode(doc['criteria']).encode()).hexdigest(),
                  'verified_at': time.time()}
        return self.put(doc['owner'], doc['workspace'], 'job.' + doc['id'],
                        'Previously verified task: ' + doc['goal'][:400] +
                        '. Re-read all evidence before relying on this result.', source)
